/**
 * ポート属性の日次同期（W1 プラン §6.9、PR F）。
 *
 * `station_information` を 1 日 1 回取得し、SCD2 で `station_attributes` に入れる。
 * 収集（`collect.ts`）と同じ規律に従う。
 *
 *   * 受信バイト列の保存は**検証より先**（W1-26）。生 JSON は一次ソースで、
 *     その保全が検証の成否に依存してはいけない
 *   * どの経路を通っても最後に記録を残す。記録が残らない失敗を作らない
 *   * 例外は `JobError` に詰め替える。URL とトークンは載せない（W1-21）
 *
 * 収集と違うのは 2 点。**条件付き取得をしない**（1 日 1 回なので節約する意味が薄く、
 * `feed_state.last_etag` は status のもの）。**記録先が `job_runs`**（`feed_fetch_log` は
 * status 専用で feed 列を持たず、混ぜると取得率の指標が汚れる）。
 */
import { normalizeStationInformation, parseStationInformationFeed } from "@bikechance/gbfs-core";
import type { FeedName, SystemId } from "@bikechance/shared";
import type { AttributeRow, AttributesPort, UpsertAttributesResult } from "./attributes-port";
import { JobError, httpJobFailure, isJobError, toJobFailure, type JobFailure } from "./errors";
import { parseFeedJson, readLastUpdatedFrom } from "./feed-timestamp";
import { fetchOdptFeed, type OdptFeedResponse } from "./odpt-fetch";
import { saveRawFeed, type RawUploader } from "./storage";

const HTTP_OK = 200;
const MS_PER_S = 1000;

const SYNCED_FEED: FeedName = "station_information";

export type SyncStationsSummary = {
  readonly ok: boolean;
  readonly system: SystemId;
  readonly status: UpsertAttributesResult["status"] | "error";
  /** フィードの `last_updated`（ISO 8601）。取得できなければ null。 */
  readonly observed_at: string | null;
  readonly http_status: number | null;
  readonly bytes: number | null;
  readonly gzip_bytes: number | null;
  readonly stored_path: string | null;
  readonly counts: Omit<UpsertAttributesResult, "status"> | null;
  readonly duration_ms: number;
  readonly warnings: Readonly<Record<string, number>> | null;
  readonly error: JobFailure | null;
};

export type SyncStationsParams = {
  readonly db: AttributesPort;
  readonly upload: RawUploader;
  readonly system_id: SystemId;
  readonly token: string;
  readonly contact_email: string;
  readonly now: Date;
};

/** 途中経過。どの段で終わっても、記録に必要な材料が揃っている形にする。 */
type Progress = {
  readonly status: SyncStationsSummary["status"];
  readonly response: OdptFeedResponse | null;
  readonly observed_at: string | null;
  readonly gzip_bytes: number | null;
  readonly stored_path: string | null;
  readonly counts: SyncStationsSummary["counts"];
  readonly warnings: Readonly<Record<string, number>> | null;
};

const EMPTY_PROGRESS: Progress = {
  status: "error",
  response: null,
  observed_at: null,
  gzip_bytes: null,
  stored_path: null,
  counts: null,
  warnings: null,
};

const toEpochSeconds = (date: Date): number => Math.floor(date.getTime() / MS_PER_S);

/** 検証。失敗は取り込みの中止であって、保存の取り消しではない。 */
const validateFeed = (document: unknown) => {
  const parsed = parseStationInformationFeed(document);
  if (!parsed.ok) {
    throw new JobError({
      phase: "parse",
      error_name: "InvalidFeed",
      http_status: null,
      message: `station_information の検証に失敗した: ${parsed.issues.join(" / ")}`,
    });
  }
  return normalizeStationInformation(parsed.feed);
};

const toRows = (
  stations: ReturnType<typeof normalizeStationInformation>["stations"],
): readonly AttributeRow[] =>
  stations.map((station) => ({
    station_id: station.station_id,
    name: station.name,
    lat: station.lat,
    lon: station.lon,
    capacity: station.capacity,
    geo_suspect: station.geo_suspect,
    raw: station.raw,
  }));

const runSync = async (
  params: SyncStationsParams,
  onProgress: (progress: Progress) => void,
): Promise<Progress> => {
  // 1. 取得
  const response = await fetchOdptFeed({
    system_id: params.system_id,
    feed: SYNCED_FEED,
    token: params.token,
    contact_email: params.contact_email,
    if_none_match: null,
  });
  onProgress({ ...EMPTY_PROGRESS, response });

  if (response.http_status !== HTTP_OK) {
    throw new JobError(
      httpJobFailure({
        phase: "fetch",
        http_status: response.http_status,
        detail: `ODPT が想定外のステータスを返した (${response.http_status})`,
      }),
    );
  }

  // 2. 保存が先（W1-26）。同じ last_updated なら同じパスになり、重複は正常系
  const document = parseFeedJson(response.body);
  const last_updated = readLastUpdatedFrom(document);
  const stored = await saveRawFeed({
    upload: params.upload,
    system_id: params.system_id,
    feed: SYNCED_FEED,
    epoch_s: last_updated ?? toEpochSeconds(params.now),
    body: response.body,
  });
  const base: Progress = {
    ...EMPTY_PROGRESS,
    response,
    gzip_bytes: stored.gzip_bytes,
    stored_path: stored.path,
  };
  onProgress(base);

  // 3. 検証・正規化
  const normalized = validateFeed(document);
  const warnings = { ...normalized.warnings };
  const withWarnings: Progress = {
    ...base,
    observed_at: new Date(normalized.observed_at_s * MS_PER_S).toISOString(),
    warnings,
  };
  onProgress(withWarnings);

  // 4. 取り込み（SCD2）
  const { status, ...counts } = await params.db.upsertAttributes({
    system_id: params.system_id,
    fetched_at: params.now,
    rows: toRows(normalized.stations),
  });
  return { ...withWarnings, status, counts: status === "ok" ? counts : null };
};

const toSummary = (
  params: SyncStationsParams,
  progress: Progress,
  failure: JobFailure | null,
  duration_ms: number,
): SyncStationsSummary => ({
  ok: failure === null,
  system: params.system_id,
  status: failure === null ? progress.status : "error",
  observed_at: progress.observed_at,
  http_status: progress.response?.http_status ?? failure?.http_status ?? null,
  bytes: progress.response?.bytes ?? null,
  gzip_bytes: progress.gzip_bytes,
  stored_path: progress.stored_path,
  counts: progress.counts,
  duration_ms,
  warnings: progress.warnings,
  error: failure,
});

/** `job_runs.detail` に入れる要約。URL とトークンは含めない。 */
const toDetail = (summary: SyncStationsSummary): Readonly<Record<string, unknown>> => ({
  system: summary.system,
  status: summary.status,
  observed_at: summary.observed_at,
  bytes: summary.bytes,
  gzip_bytes: summary.gzip_bytes,
  stored_path: summary.stored_path,
  duration_ms: summary.duration_ms,
  ...(summary.counts ?? {}),
  ...(summary.warnings === null ? {} : { warnings: summary.warnings }),
  ...(summary.error === null ? {} : { error: `${summary.error.phase}: ${summary.error.message}` }),
});

/**
 * 記録は「できなければ諦める」。ここで例外を上げると、元の失敗が記録の失敗に
 * すり替わって何が起きたか分からなくなる。
 */
const recordQuietly = async (
  params: SyncStationsParams,
  run_id: number | null,
  summary: SyncStationsSummary,
): Promise<void> => {
  if (run_id === null) {
    return;
  }
  try {
    await params.db.jobFinished(run_id, summary.ok ? "ok" : "failed", toDetail(summary));
  } catch (cause) {
    const failure = toJobFailure({ phase: "ingest", cause, secrets: [params.token] });
    console.error(`job_finished に失敗した: ${failure.error_name}: ${failure.message}`);
  }
};

export const jobNameFor = (system_id: SystemId): string => `sync_stations:${system_id}`;

export const syncStations = async (params: SyncStationsParams): Promise<SyncStationsSummary> => {
  const started_ms = Date.now();
  // 途中で失敗したときに「どこまで進んでいたか」を記録するための唯一の可変状態
  let reached: Progress = EMPTY_PROGRESS;
  let run_id: number | null = null;

  try {
    run_id = await params.db.jobStarted(jobNameFor(params.system_id));
  } catch (cause) {
    // 記録を始められなくても同期そのものは行う。記録の不調で属性を失わない
    const failure = toJobFailure({ phase: "ingest", cause, secrets: [params.token] });
    console.error(`job_started に失敗した: ${failure.error_name}: ${failure.message}`);
  }

  const summary = await runSync(params, (progress) => {
    reached = progress;
  }).then(
    (progress) => toSummary(params, progress, null, Date.now() - started_ms),
    (cause: unknown) =>
      toSummary(
        params,
        reached,
        isJobError(cause)
          ? cause.failure
          : toJobFailure({ phase: "unknown", cause, secrets: [params.token] }),
        Date.now() - started_ms,
      ),
  );

  await recordQuietly(params, run_id, summary);
  return summary;
};
