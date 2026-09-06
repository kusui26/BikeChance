/**
 * PR 0 の収集：ODPT → gzip → Storage（W1 プラン §6.2）。
 *
 * **DB には一切書かない。** `feed_state`・`status_snapshots`・`feed_fetch_log` は
 * まだ存在しない。ETag の条件付き要求・重複排除・配列化・異常ガードは PR D で足す。
 *
 * この段階で守りたいのは 1 つだけ：**生 JSON を落とさないこと**。生 JSON は一次ソースで、
 * Postgres の配列はここから再構築できる（開発プラン D-03、W1 プラン W1-22）。
 */
import type { FeedName, SystemId } from "@bikechance/shared";
import { JobError, httpJobFailure, isJobError, toJobFailure, type JobFailure } from "./errors";
import { readLastUpdated } from "./feed-timestamp";
import { fetchOdptFeed, type OdptEndpoint, type OdptFeedResponse } from "./odpt-fetch";
import { saveRawFeed, type RawSaveOutcome, type RawUploader } from "./storage";

const HTTP_OK = 200;
const MS_PER_S = 1000;

/** `last_updated` が読めず、取得時刻をパスに使ったことを示す。 */
export const WARNING_LAST_UPDATED_UNREADABLE = "last_updated_unreadable";

/** 認証付きが失敗し公開エンドポイントで取得したことを示す。 */
export const WARNING_PUBLIC_FALLBACK = "public_endpoint_fallback";

export const COLLECT_RESULTS = ["saved", "duplicate", "error"] as const;
export type CollectResult = (typeof COLLECT_RESULTS)[number];

export type CollectSummary = {
  readonly ok: boolean;
  readonly system: SystemId;
  readonly feed: FeedName;
  readonly result: CollectResult;
  /** 保存に使った観測時刻（ISO 8601）。`last_updated` か、読めなければ取得時刻。 */
  readonly observed_at: string | null;
  readonly http_status: number | null;
  readonly endpoint: OdptEndpoint | null;
  readonly bytes: number | null;
  readonly gzip_bytes: number | null;
  readonly stored_path: string | null;
  readonly duration_ms: number;
  readonly warnings: readonly string[];
  /** 失敗時のみ。URL とトークンは含まない（W1-21）。 */
  readonly error: JobFailure | null;
};

export type CollectParams = {
  readonly upload: RawUploader;
  readonly system_id: SystemId;
  readonly feed: FeedName;
  readonly token: string;
  readonly contact_email: string;
  readonly now: Date;
};

type CollectOutcome = {
  readonly response: OdptFeedResponse;
  readonly save: RawSaveOutcome;
  readonly observed_at_epoch_s: number;
  readonly warnings: readonly string[];
};

const toEpochSeconds = (date: Date): number => Math.floor(date.getTime() / MS_PER_S);

const collectWarnings = (response: OdptFeedResponse, last_updated: number | null): string[] => [
  ...(last_updated === null ? [WARNING_LAST_UPDATED_UNREADABLE] : []),
  ...(response.fallback_reason === null
    ? []
    : [`${WARNING_PUBLIC_FALLBACK}: ${response.fallback_reason}`]),
];

/** 正常系。失敗は JobError として投げる。 */
const runCollect = async (params: CollectParams): Promise<CollectOutcome> => {
  const response = await fetchOdptFeed(params);
  if (response.http_status !== HTTP_OK) {
    throw new JobError(
      httpJobFailure({
        phase: "fetch",
        http_status: response.http_status,
        detail: `ODPT が想定外のステータスを返した (${response.http_status})`,
      }),
    );
  }
  const last_updated = readLastUpdated(response.body);
  const observed_at_epoch_s = last_updated ?? toEpochSeconds(params.now);
  const save = await saveRawFeed({
    upload: params.upload,
    system_id: params.system_id,
    feed: params.feed,
    epoch_s: observed_at_epoch_s,
    body: response.body,
  });
  return { response, save, observed_at_epoch_s, warnings: collectWarnings(response, last_updated) };
};

const successSummary = (
  params: CollectParams,
  outcome: CollectOutcome,
  duration_ms: number,
): CollectSummary => ({
  ok: true,
  system: params.system_id,
  feed: params.feed,
  result: outcome.save.result,
  observed_at: new Date(outcome.observed_at_epoch_s * MS_PER_S).toISOString(),
  http_status: outcome.response.http_status,
  endpoint: outcome.response.endpoint,
  bytes: outcome.response.bytes,
  gzip_bytes: outcome.save.gzip_bytes,
  stored_path: outcome.save.path,
  duration_ms,
  warnings: outcome.warnings,
  error: null,
});

const failureSummary = (
  params: CollectParams,
  cause: unknown,
  duration_ms: number,
): CollectSummary => {
  const failure = isJobError(cause)
    ? cause.failure
    : toJobFailure({ phase: "fetch", cause, secrets: [params.token] });
  return {
    ok: false,
    system: params.system_id,
    feed: params.feed,
    result: "error",
    observed_at: null,
    http_status: failure.http_status,
    endpoint: null,
    bytes: null,
    gzip_bytes: null,
    stored_path: null,
    duration_ms,
    warnings: [],
    error: failure,
  };
};

export const collectRawFeed = async (params: CollectParams): Promise<CollectSummary> => {
  const started_ms = Date.now();
  try {
    const outcome = await runCollect(params);
    return successSummary(params, outcome, Date.now() - started_ms);
  } catch (cause) {
    return failureSummary(params, cause, Date.now() - started_ms);
  }
};
