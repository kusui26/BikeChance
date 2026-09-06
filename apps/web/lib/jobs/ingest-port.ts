/**
 * 取り込み RPC（PR B、§11.3）への差し替え可能な入り口。
 *
 * `RawUploader`（PR 0）と同じ考え方で、DB とのやりとりを 3 つの関数に絞る。
 * こうすると上位のロジックが supabase-js に依存せず、テストで分岐を全部通せる。
 * 本番の実装は `createSupabaseIngestPort` ただ 1 つ。
 */
import type { IngestArgs } from "@bikechance/gbfs-core";
import type { SystemId } from "@bikechance/shared";
import type { SupabaseClient } from "@supabase/supabase-js";
import { JobError, toJobFailure } from "./errors";

/** `begin_fetch` の戻り値（§11.3）。 */
export type BeginFetchResult = {
  readonly claimed: boolean;
  /** 前回取り込みに成功したスナップショットの ETag。条件付き要求に使う。 */
  readonly last_etag: string | null;
  /** 前回取り込みに成功したスナップショットの `last_updated`（ISO 8601）。 */
  readonly last_observed_at: string | null;
};

/** `ingest_snapshot` の戻り値。`inserted` / `duplicate` / `locked` はいずれも正常系。 */
export type IngestResult = {
  readonly status: "inserted" | "duplicate" | "locked";
  readonly n_stations: number | null;
  readonly n_new_stations: number | null;
  readonly n_changed: number | null;
  readonly array_length: number | null;
  readonly is_anomalous: boolean | null;
};

/** `finish_fetch` に渡す記録（§11.6）。URL とトークンは含めない。 */
export type FetchLogEntry = {
  readonly ok: boolean;
  readonly result: string;
  readonly source: string;
  readonly fetched_at: string;
  readonly endpoint: string | null;
  readonly http_status: number | null;
  readonly bytes: number | null;
  readonly duration_ms: number;
  readonly n_stations: number | null;
  readonly ratelimit_remaining_day: number | null;
  readonly error: string | null;
  readonly warnings: Readonly<Record<string, number>> | null;
};

export type IngestPort = {
  readonly beginFetch: (system_id: SystemId) => Promise<BeginFetchResult>;
  readonly ingestSnapshot: (args: IngestArgs) => Promise<IngestResult>;
  readonly finishFetch: (system_id: SystemId, entry: FetchLogEntry) => Promise<void>;
};

const readString = (source: Record<string, unknown>, key: string): string | null => {
  const value = source[key];
  return typeof value === "string" ? value : null;
};

const readNumber = (source: Record<string, unknown>, key: string): number | null => {
  const value = source[key];
  return typeof value === "number" ? value : null;
};

const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value))
    : {};

const toIngestStatus = (value: string | null): IngestResult["status"] => {
  if (value === "inserted" || value === "duplicate" || value === "locked") {
    return value;
  }
  throw new JobError({
    phase: "ingest",
    error_name: "UnexpectedIngestStatus",
    http_status: null,
    message: `ingest_snapshot が想定外の status を返した: ${value ?? "(なし)"}`,
  });
};

/** PostgREST のエラーを、URL を含まない JobFailure に詰め替える。 */
const raiseRpcError = (rpc: string, error: unknown): never => {
  const failure = toJobFailure({ phase: "ingest", cause: error });
  throw new JobError({ ...failure, message: `${rpc}: ${failure.message}` });
};

export const createSupabaseIngestPort = (client: SupabaseClient): IngestPort => ({
  beginFetch: async (system_id) => {
    const { data, error } = await client.rpc("begin_fetch", { p_system_id: system_id });
    if (error !== null) {
      raiseRpcError("begin_fetch", error);
    }
    const row = asRecord(data);
    return {
      claimed: row["claimed"] === true,
      last_etag: readString(row, "last_etag"),
      last_observed_at: readString(row, "last_observed_at"),
    };
  },

  ingestSnapshot: async (args) => {
    const { data, error } = await client.rpc("ingest_snapshot", args);
    if (error !== null) {
      raiseRpcError("ingest_snapshot", error);
    }
    const row = asRecord(data);
    return {
      status: toIngestStatus(readString(row, "status")),
      n_stations: readNumber(row, "n_stations"),
      n_new_stations: readNumber(row, "n_new_stations"),
      n_changed: readNumber(row, "n_changed"),
      array_length: readNumber(row, "array_length"),
      is_anomalous: typeof row["is_anomalous"] === "boolean" ? row["is_anomalous"] : null,
    };
  },

  finishFetch: async (system_id, entry) => {
    const { error } = await client.rpc("finish_fetch", { p_system_id: system_id, p_log: entry });
    if (error !== null) {
      raiseRpcError("finish_fetch", error);
    }
  },
});
