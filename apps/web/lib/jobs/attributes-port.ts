/**
 * 属性同期の RPC（PR F、§6.9）への差し替え可能な入り口。
 *
 * `IngestPort` と同じ考え方。DB とのやりとりを 3 つの関数に絞ると、上位のロジックが
 * supabase-js に依存せず、テストで分岐を全部通せる。本番の実装は 1 つだけ。
 *
 * **記録は `job_runs` に残す。** `feed_fetch_log` は status 専用で feed 列を持たず、
 * 属性同期を混ぜると取得率と誤検知の指標が汚れる。
 */
import type { SystemId } from "@bikechance/shared";
import type { SupabaseClient } from "@supabase/supabase-js";
import { JobError, toJobFailure } from "./errors";

/** RPC に渡す 1 ポート分。`raw` は GBFS のオブジェクト全体。 */
export type AttributeRow = {
  readonly station_id: string;
  readonly name: string;
  readonly lat: number;
  readonly lon: number;
  readonly capacity: number | null;
  readonly geo_suspect: boolean;
  readonly raw: unknown;
};

/** `upsert_station_attributes` の戻り値。`locked` も正常系（多重起動を弾いただけ）。 */
export type UpsertAttributesResult = {
  readonly status: "ok" | "locked";
  readonly n_input: number;
  readonly n_new_stations: number;
  readonly n_changed: number;
  readonly n_unchanged: number;
  readonly n_versions_added: number;
  readonly n_geo_suspect: number;
  readonly n_skipped_older: number;
};

export type AttributesPort = {
  readonly upsertAttributes: (params: {
    readonly system_id: SystemId;
    readonly fetched_at: Date;
    readonly rows: readonly AttributeRow[];
  }) => Promise<UpsertAttributesResult>;
  /** `job_runs` に開始を書き、行 id を返す。 */
  readonly jobStarted: (job_name: string) => Promise<number>;
  readonly jobFinished: (
    id: number,
    status: "ok" | "failed",
    detail: Readonly<Record<string, unknown>>,
  ) => Promise<void>;
};

const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value))
    : {};

const readCount = (source: Record<string, unknown>, key: string): number => {
  const value = source[key];
  return typeof value === "number" ? value : 0;
};

const toStatus = (value: unknown): UpsertAttributesResult["status"] => {
  if (value === "ok" || value === "locked") {
    return value;
  }
  throw new JobError({
    phase: "ingest",
    error_name: "UnexpectedUpsertStatus",
    http_status: null,
    message: `upsert_station_attributes が想定外の status を返した: ${String(value)}`,
  });
};

const raiseRpcError = (rpc: string, error: unknown): never => {
  const failure = toJobFailure({ phase: "ingest", cause: error });
  throw new JobError({ ...failure, message: `${rpc}: ${failure.message}` });
};

export const createSupabaseAttributesPort = (client: SupabaseClient): AttributesPort => ({
  upsertAttributes: async ({ system_id, fetched_at, rows }) => {
    const { data, error } = await client.rpc("upsert_station_attributes", {
      p_system_id: system_id,
      p_fetched_at: fetched_at.toISOString(),
      p_rows: rows,
    });
    if (error !== null) {
      raiseRpcError("upsert_station_attributes", error);
    }
    const row = asRecord(data);
    return {
      status: toStatus(row["status"]),
      n_input: readCount(row, "n_input"),
      n_new_stations: readCount(row, "n_new_stations"),
      n_changed: readCount(row, "n_changed"),
      n_unchanged: readCount(row, "n_unchanged"),
      n_versions_added: readCount(row, "n_versions_added"),
      n_geo_suspect: readCount(row, "n_geo_suspect"),
      n_skipped_older: readCount(row, "n_skipped_older"),
    };
  },

  jobStarted: async (job_name) => {
    const { data, error } = await client.rpc("job_started", { p_job_name: job_name });
    if (error !== null) {
      raiseRpcError("job_started", error);
    }
    if (typeof data !== "number") {
      throw new JobError({
        phase: "ingest",
        error_name: "UnexpectedJobId",
        http_status: null,
        message: "job_started が行 id を返さなかった",
      });
    }
    return data;
  },

  jobFinished: async (id, status, detail) => {
    const { error } = await client.rpc("job_finished", {
      p_id: id,
      p_status: status,
      p_detail: detail,
    });
    if (error !== null) {
      raiseRpcError("job_finished", error);
    }
  },
});
