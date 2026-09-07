/**
 * 天気アーカイブが使う DB の入り口（W2 プラン §5.3）。
 *
 * `IngestPort` / `AttributesPort` と同じ考え方。DB とのやりとりを 3 つの関数に絞ると、
 * 上位のロジックが supabase-js に依存せず、テストで分岐を全部通せる。
 */
import { WEATHER_GRID_LAT_STEP, WEATHER_GRID_LON_STEP } from "@bikechance/shared";
import type { SupabaseClient } from "@supabase/supabase-js";
import { JobError, toJobFailure } from "./errors";
import type { WeatherCell } from "./weather-fetch";

export type WeatherPort = {
  /** ポートが分布する気象格子。刻みは共有定数を SQL に渡す（値の出どころを 1 箇所にする）。 */
  readonly listGridCells: () => Promise<readonly WeatherCell[]>;
  readonly jobStarted: (job_name: string) => Promise<number>;
  readonly jobFinished: (
    id: number,
    status: "ok" | "failed",
    detail: Readonly<Record<string, unknown>>,
  ) => Promise<void>;
};

const raiseRpcError = (rpc: string, error: unknown): never => {
  const failure = toJobFailure({ phase: "ingest", cause: error });
  throw new JobError({ ...failure, message: `${rpc}: ${failure.message}` });
};

const isCell = (value: unknown): value is WeatherCell =>
  typeof value === "object" &&
  value !== null &&
  typeof Reflect.get(value, "lat") === "number" &&
  typeof Reflect.get(value, "lon") === "number";

const toCells = (data: unknown): readonly WeatherCell[] => {
  if (!Array.isArray(data)) {
    throw new JobError({
      phase: "ingest",
      error_name: "UnexpectedGridShape",
      http_status: null,
      message: "weather_grid_cells が配列を返さなかった",
    });
  }
  const rows: readonly unknown[] = data;
  return rows.filter(isCell).map((cell) => ({ lat: cell.lat, lon: cell.lon }));
};

export const createSupabaseWeatherPort = (client: SupabaseClient): WeatherPort => ({
  listGridCells: async () => {
    const { data, error } = await client.rpc("weather_grid_cells", {
      p_lat_step: WEATHER_GRID_LAT_STEP,
      p_lon_step: WEATHER_GRID_LON_STEP,
    });
    if (error !== null) {
      raiseRpcError("weather_grid_cells", error);
    }
    return toCells(data);
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
