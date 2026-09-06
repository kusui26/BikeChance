/**
 * 収集の一連の手順（W1 プラン §6.6、PR D）。
 *
 * PR 0 は「取得して Storage に置く」だけだった。ここに ETag による条件付き取得、
 * 検証・正規化、RPC による取り込みを足す。
 *
 * **順序の要**：受信バイト列の保存は**検証より先**に行う（W1-26）。生 JSON は一次ソースで、
 * 後から何度でも再処理できることが唯一の保険なのに、その保険が検証の成否に依存していては
 * 意味がない。検証に失敗しても生データは残る。
 *
 * どの経路を通っても最後に `finish_fetch` で記録を残す。記録が残らない失敗を作らない。
 */
import {
  buildIngestArgs,
  normalizeStationStatus,
  parseStationStatusFeed,
} from "@bikechance/gbfs-core";
import type { FeedName, SystemId } from "@bikechance/shared";
import { JobError, httpJobFailure, isJobError, toJobFailure, type JobFailure } from "./errors";
import { parseFeedJson, readLastUpdatedFrom } from "./feed-timestamp";
import type { FetchLogEntry, IngestPort } from "./ingest-port";
import { fetchOdptFeed, type OdptEndpoint, type OdptFeedResponse } from "./odpt-fetch";
import { saveRawFeed, type RawUploader } from "./storage";

const HTTP_OK = 200;
const HTTP_NOT_MODIFIED = 304;
const MS_PER_S = 1000;

/** 誰がこの実行を起こしたか（§5 の 25）。ウォッチドッグの到達を見分ける。 */
export const COLLECT_SOURCES = ["cron", "watchdog", "manual"] as const;
export type CollectSource = (typeof COLLECT_SOURCES)[number];

export const isCollectSource = (value: string): value is CollectSource =>
  COLLECT_SOURCES.some((source) => source === value);

/** 応答の `result`（§11.6）。`error` 以外はすべて 200 で返す。 */
export const COLLECT_RESULTS = [
  "inserted",
  "duplicate",
  "unchanged",
  "skipped_recent",
  "locked",
  "error",
] as const;
export type CollectResult = (typeof COLLECT_RESULTS)[number];

export type CollectSummary = {
  readonly ok: boolean;
  readonly system: SystemId;
  readonly result: CollectResult;
  readonly source: CollectSource;
  /** 取り込んだスナップショットの `last_updated`（ISO 8601）。取り込まなければ null。 */
  readonly observed_at: string | null;
  readonly http_status: number | null;
  readonly endpoint: OdptEndpoint | null;
  readonly bytes: number | null;
  readonly gzip_bytes: number | null;
  readonly stored_path: string | null;
  readonly n_stations: number | null;
  readonly n_new_stations: number | null;
  readonly n_changed: number | null;
  readonly is_anomalous: boolean | null;
  readonly ratelimit_remaining_day: number | null;
  readonly duration_ms: number;
  readonly warnings: Readonly<Record<string, number>> | null;
  /** 失敗時のみ。URL とトークンは含まない（W1-21）。 */
  readonly error: JobFailure | null;
};

export type CollectParams = {
  readonly db: IngestPort;
  readonly upload: RawUploader;
  readonly system_id: SystemId;
  readonly feed: FeedName;
  readonly source: CollectSource;
  readonly token: string;
  readonly contact_email: string;
  readonly now: Date;
};

/** 途中経過。どの段で終わっても、記録に必要な材料が揃っている形にする。 */
type Progress = {
  readonly result: CollectResult;
  readonly response: OdptFeedResponse | null;
  readonly observed_at: string | null;
  readonly gzip_bytes: number | null;
  readonly stored_path: string | null;
  readonly n_stations: number | null;
  readonly n_new_stations: number | null;
  readonly n_changed: number | null;
  readonly is_anomalous: boolean | null;
  readonly warnings: Readonly<Record<string, number>> | null;
};

const EMPTY_PROGRESS: Progress = {
  result: "unchanged",
  response: null,
  observed_at: null,
  gzip_bytes: null,
  stored_path: null,
  n_stations: null,
  n_new_stations: null,
  n_changed: null,
  is_anomalous: null,
  warnings: null,
};

const toEpochSeconds = (date: Date): number => Math.floor(date.getTime() / MS_PER_S);

/** 前回取り込んだスナップショット以下なら、取り込む価値がない（後退・同一）。 */
const isStale = (observed_at_s: number, last_observed_at: string | null): boolean =>
  last_observed_at !== null && observed_at_s * MS_PER_S <= Date.parse(last_observed_at);

/** 手順 6：受信バイト列をそのまま保存する。検証より先に行う（W1-26）。 */
const storeRawFeed = async (
  params: CollectParams,
  response: OdptFeedResponse,
  last_updated: number | null,
): Promise<{ epoch_s: number; gzip_bytes: number; path: string }> => {
  const epoch_s = last_updated ?? toEpochSeconds(params.now);
  const save = await saveRawFeed({
    upload: params.upload,
    system_id: params.system_id,
    feed: params.feed,
    epoch_s,
    body: response.body,
  });
  return { epoch_s, gzip_bytes: save.gzip_bytes, path: save.path };
};

/** 手順 7：検証・正規化。失敗は取り込みの中止であって、保存の取り消しではない。 */
const validateFeed = (document: unknown) => {
  const parsed = parseStationStatusFeed(document);
  if (!parsed.ok) {
    throw new JobError({
      phase: "parse",
      error_name: "InvalidFeed",
      http_status: null,
      message: `フィードの検証に失敗した: ${parsed.issues.join(" / ")}`,
    });
  }
  return normalizeStationStatus(parsed.feed);
};

/**
 * 手順 3〜8。失敗は JobError として投げ、呼び出し側が記録する。
 *
 * `onProgress` はここまでに分かったことを外へ渡す。途中で失敗しても
 * 「取得は済んでいた」「生 JSON は保存できていた」を記録に残せるようにするため。
 */
const runCollect = async (
  params: CollectParams,
  onProgress: (progress: Progress) => void,
): Promise<Progress> => {
  // 3. claim
  const claim = await params.db.beginFetch(params.system_id);
  if (!claim.claimed) {
    return { ...EMPTY_PROGRESS, result: "skipped_recent" };
  }

  // 4. 取得（前回取り込みに成功した ETag で条件付き要求）
  const response = await fetchOdptFeed({
    system_id: params.system_id,
    feed: params.feed,
    token: params.token,
    contact_email: params.contact_email,
    if_none_match: claim.last_etag,
  });

  onProgress({ ...EMPTY_PROGRESS, result: "error", response });

  // 5. 変わっていない
  if (response.http_status === HTTP_NOT_MODIFIED) {
    return { ...EMPTY_PROGRESS, result: "unchanged", response };
  }
  if (response.http_status !== HTTP_OK) {
    throw new JobError(
      httpJobFailure({
        phase: "fetch",
        http_status: response.http_status,
        detail: `ODPT が想定外のステータスを返した (${response.http_status})`,
      }),
    );
  }

  // 6. 保存が先（W1-26）
  const document = parseFeedJson(response.body);
  const last_updated = readLastUpdatedFrom(document);
  const stored = await storeRawFeed(params, response, last_updated);
  const base = {
    ...EMPTY_PROGRESS,
    response,
    gzip_bytes: stored.gzip_bytes,
    stored_path: stored.path,
  };
  onProgress({ ...base, result: "error" });

  // 7. 検証・正規化と、後退・同一の判定
  const normalized = validateFeed(document);
  const warnings = { ...normalized.warnings };
  if (isStale(normalized.observed_at_s, claim.last_observed_at)) {
    return { ...base, result: "unchanged", warnings };
  }

  // 8. 取り込み
  const ingest = await params.db.ingestSnapshot(
    buildIngestArgs({
      system_id: params.system_id,
      feed: normalized,
      fetched_at: params.now,
      etag: response.etag,
      raw_path: stored.path,
    }),
  );
  return {
    ...base,
    result: ingest.status,
    observed_at: new Date(normalized.observed_at_s * MS_PER_S).toISOString(),
    n_stations: ingest.n_stations,
    n_new_stations: ingest.n_new_stations,
    n_changed: ingest.n_changed,
    is_anomalous: ingest.is_anomalous,
    warnings,
  };
};

const toSummary = (
  params: CollectParams,
  progress: Progress,
  failure: JobFailure | null,
  duration_ms: number,
): CollectSummary => ({
  ok: failure === null,
  system: params.system_id,
  result: failure === null ? progress.result : "error",
  source: params.source,
  observed_at: progress.observed_at,
  http_status: progress.response?.http_status ?? failure?.http_status ?? null,
  endpoint: progress.response?.endpoint ?? null,
  bytes: progress.response?.bytes ?? null,
  gzip_bytes: progress.gzip_bytes,
  stored_path: progress.stored_path,
  n_stations: progress.n_stations,
  n_new_stations: progress.n_new_stations,
  n_changed: progress.n_changed,
  is_anomalous: progress.is_anomalous,
  ratelimit_remaining_day: progress.response?.ratelimit_remaining_day ?? null,
  duration_ms,
  warnings: progress.warnings,
  error: failure,
});

const toLogEntry = (summary: CollectSummary, fetched_at: Date): FetchLogEntry => ({
  ok: summary.ok,
  result: summary.result,
  source: summary.source,
  fetched_at: fetched_at.toISOString(),
  endpoint: summary.endpoint,
  http_status: summary.http_status,
  bytes: summary.bytes,
  duration_ms: summary.duration_ms,
  n_stations: summary.n_stations,
  ratelimit_remaining_day: summary.ratelimit_remaining_day,
  error: summary.error === null ? null : `${summary.error.phase}: ${summary.error.message}`,
  warnings: summary.warnings,
});

/**
 * 記録は「できなければ諦める」。ここで例外を上げると、元の失敗が記録の失敗に
 * すり替わって何が起きたか分からなくなる。応答のステータスは元の結果のまま返す。
 */
const recordQuietly = async (params: CollectParams, summary: CollectSummary): Promise<void> => {
  try {
    await params.db.finishFetch(params.system_id, toLogEntry(summary, params.now));
  } catch (cause) {
    const failure = toJobFailure({ phase: "ingest", cause, secrets: [params.token] });
    console.error(`finish_fetch に失敗した: ${failure.error_name}: ${failure.message}`);
  }
};

export const collect = async (params: CollectParams): Promise<CollectSummary> => {
  const started_ms = Date.now();
  // 途中で失敗したときに「どこまで進んでいたか」を記録するための唯一の可変状態。
  // これが無いと、生 JSON を保存できていた事実がエラー時に失われる
  let reached: Progress = EMPTY_PROGRESS;

  const summary = await runCollect(params, (progress) => {
    reached = progress;
  }).then(
    (progress) => toSummary(params, progress, null, Date.now() - started_ms),
    (cause: unknown) =>
      toSummary(
        params,
        reached,
        isJobError(cause)
          ? cause.failure
          : // 各段で包んでいるので、ここへ来る例外は想定外のもの。phase を偽らない
            toJobFailure({ phase: "unknown", cause, secrets: [params.token] }),
        Date.now() - started_ms,
      ),
  );
  await recordQuietly(params, summary);
  return summary;
};
