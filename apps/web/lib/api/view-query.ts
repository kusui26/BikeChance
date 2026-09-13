/**
 * 公開 API が読むビューと、その絞り込みの組み立て（純粋）。
 *
 * **副作用を持たない。** supabase-js に渡す前の「何をどう絞るか」だけをここで決める。
 * bbox の 4 辺を列と演算子に写す部分は取り違えやすく（南北と東西を入れ替えても
 * 型は通る）、静かに間違った範囲を返す類の誤りになるので、純粋な関数にして固定する。
 */
import { SYSTEM_IDS, type Bbox, type SystemId } from "@bikechance/shared";
import { z } from "zod";

/** ビューの名前はここだけに書く。基底テーブルは公開経路のどこからも名指ししない。 */
export const FEEDS_VIEW = "v1_feeds";
export const STATIONS_VIEW = "v1_stations_current";
/** 代替候補（`/v1/trip-check`。migration 0039、W4-26）。 */
export const NEIGHBORS_VIEW = "v1_station_neighbors";
/** 直近 24 時間の実績（`/v1/stations/{system}/{id}`。migration 0046）。 */
export const HOURLY_VIEW = "v1_station_hourly";

/** `v1_feeds` から読む列。`select("*")` にしないのは、列が増えたときに気づけるようにするため。 */
export const FEED_COLUMNS =
  "system_id,display_name,expected_cadence_s,poll_interval_s,capacity_is_dynamic,last_observed_at," +
  "forecast_model_version,forecast_generated_at";

export const STATION_COLUMNS =
  "system_id,station_id,name,lat,lon,capacity,capacity_est,capacity_days," +
  "bikes,docks,is_installed,is_renting,is_returning," +
  "is_present,last_changed_at," +
  "forecast_horizons_min,forecast_p_bike_x1000,forecast_p_dock_x1000,forecast_confidence," +
  "forecast_base_observed_at,forecast_model_version,forecast_generated_at";

export const NEIGHBOR_COLUMNS = "station_id,nb_station_id,distance_m";

/** `v1_station_hourly` から読む列（0046）。`system_id` は絞り込みで分かっているので読まない。 */
export const HOURLY_COLUMNS = "station_id,hour_start,n,bikes_mean,docks_mean";

/** 絞り込み 1 つ。`op` は supabase-js の同名メソッドに対応する。 */
export type Filter =
  | { readonly op: "gte" | "lte"; readonly column: string; readonly value: number }
  // 時刻の下限。**数と混ぜない**——`gte` は数のための口で、型で取り違えを止める
  | { readonly op: "gte_text"; readonly column: string; readonly value: string }
  | { readonly op: "eq"; readonly column: string; readonly value: string }
  | { readonly op: "is"; readonly column: string; readonly value: boolean }
  | { readonly op: "in"; readonly column: string; readonly values: readonly string[] };

/**
 * 矩形を 4 つの範囲条件に写す。
 *
 * **緯度は south〜north、経度は west〜east。** 名前で対応が読めるように並べてある。
 */
export const bboxFilters = (bbox: Bbox): readonly Filter[] => [
  { op: "gte", column: "lat", value: bbox.south },
  { op: "lte", column: "lat", value: bbox.north },
  { op: "gte", column: "lon", value: bbox.west },
  { op: "lte", column: "lon", value: bbox.east },
];

/** システムの絞り込み。未指定なら条件を足さない。 */
export const systemFilters = (system_id: SystemId | null): readonly Filter[] =>
  system_id === null ? [] : [{ op: "eq", column: "system_id", value: system_id }];

/**
 * ID を並べてポートを引く（`/v1/trip-check`）。
 *
 * **`system_id` で絞らない。** `station_id` はシステムをまたいで衝突するので（実測で
 * 313 件）、系統の絞り込みを掛けると「別の系統にある ID を渡した」のか「どこにも無い」
 * のかが区別できない。**全部引いてから呼ぶ側で選り分ける**ことで、
 * 「その ID は別のシステムのものです」と答えられる（W4-21）。
 */
export const stationIdFilters = (station_ids: readonly string[]): readonly Filter[] => [
  { op: "in", column: "station_id", values: station_ids },
];

/**
 * 代替候補の絞り込み（W4-25）。**同一システム・指定の半径まで。**
 *
 * 2 つの端点ぶんをまとめて引く（`station_id` を並べる）。ビューは 500 m まで持っていて
 * 絞り込みを焼き付けていないので（W4-26）、ここで半径を決める。
 */
export const neighborFilters = (params: {
  readonly system_id: SystemId;
  readonly station_ids: readonly string[];
  readonly radius_m: number;
}): readonly Filter[] => [
  { op: "eq", column: "system_id", value: params.system_id },
  { op: "in", column: "station_id", values: params.station_ids },
  // **同一システムだけ。** 別事業者の自転車は借りられない（`same_system` が在る理由）
  { op: "is", column: "same_system", value: true },
  { op: "lte", column: "distance_m", value: params.radius_m },
];

/**
 * 1 ポートを名指しで引く（`/v1/stations/{system}/{station_id}`）。
 *
 * **ここは `system_id` でも絞る。** `/v1/trip-check` が絞らないのは「その ID は別の
 * システムのものです」と答えるためだが（W4-21）、詳細は**経路そのものが系統を含む**
 * ので、別系統の同じ ID が出てきたら URL と食い違う。
 */
export const oneStationFilters = (params: {
  readonly system_id: SystemId;
  readonly station_id: string;
}): readonly Filter[] => [
  { op: "eq", column: "system_id", value: params.system_id },
  { op: "eq", column: "station_id", value: params.station_id },
];

/**
 * 直近の実績の絞り込み（0046）。**時間の下限は呼ぶ側が決める。**
 *
 * ビューは 26 時間ぶん持っている（集計の遅れ 2 時間ぶんの余白）。**24 時間で切るのは
 * 読む側の判断**で、ビューに焼き付けない（`neighborFilters` が半径を焼き付けないのと
 * 同じ。W4-26）。
 */
export const hourlyFilters = (params: {
  readonly system_id: SystemId;
  readonly station_id: string;
  /** この時刻以降の行だけ。ISO 8601。 */
  readonly since: string;
}): readonly Filter[] => [
  { op: "eq", column: "system_id", value: params.system_id },
  { op: "eq", column: "station_id", value: params.station_id },
  { op: "gte_text", column: "hour_start", value: params.since },
];

/** 並びは固定する。同じ要求が同じ応答になり、差分も追える。 */
export const STATION_ORDER = ["system_id", "station_id"] as const;

const systemIdSchema = z.enum(SYSTEM_IDS);

/** `v1_feeds` の 1 行。 */
export const feedRowSchema = z.object({
  system_id: systemIdSchema,
  display_name: z.string(),
  expected_cadence_s: z.number().int().positive(),
  poll_interval_s: z.number().int().positive(),
  capacity_is_dynamic: z.boolean(),
  last_observed_at: z.string().nullable(),
  /** いま配信している予測の版。**まだ 1 度も推論していなければ null**（0033）。 */
  forecast_model_version: z.string().nullable(),
  forecast_generated_at: z.string().nullable(),
});

/**
 * `v1_stations_current` の 1 行。
 *
 * **`lat` / `lon` は NULL になりうる。** 属性の日次同期がまだ届いていないポートと、
 * 属性が失効したポートがそれに当たる（開発プラン §8.3）。bbox で引くかぎり範囲比較で
 * 自然に外れるが、**ID で引くと出てくる**（`/v1/trip-check`）。
 *
 * 実測（2026-09-11、本番）：**座標の無いポートは 10 件**あり、**そのうち 1 件は予測を
 * 持っていた**。「座標が無い＝予測も無い」ではない。
 *
 * `name` / `capacity` / `bikes` なども NULL があり得る（未取得・未観測）。
 */
export const stationRowSchema = z.object({
  system_id: systemIdSchema,
  station_id: z.string().min(1),
  name: z.string().nullable(),
  lat: z.number().nullable(),
  lon: z.number().nullable(),
  /** **固定のラック数のみ**。動的な系統は NULL（0035）。 */
  capacity: z.number().int().nullable(),
  /** **実測からの大きさ**（0045）。まだ写していなければ NULL。`capacity` とは別の数。 */
  capacity_est: z.number().int().nullable(),
  capacity_days: z.number().int().nullable(),
  bikes: z.number().int().nullable(),
  docks: z.number().int().nullable(),
  is_installed: z.boolean().nullable(),
  is_renting: z.boolean().nullable(),
  is_returning: z.boolean().nullable(),
  is_present: z.boolean(),
  last_changed_at: z.string(),
  /**
   * 予測（0033）。**予測の無いポートがあるので、すべて null になり得る。**
   *
   * 配列の長さは `horizons_min` と `p_*_x1000` でそろっているはずだが、**ここでは
   * そろっていることを要求しない**。ビューは `station_forecasts` を素直に写すだけで、
   * そろっているかを保証するのは書き手（`upsert_forecasts`）である。読む側は
   * `interpolateForecast` が長さを見て null を返す（欠けた表から数を作らない）。
   */
  forecast_horizons_min: z.array(z.number().int()).nullable(),
  forecast_p_bike_x1000: z.array(z.number().int()).nullable(),
  forecast_p_dock_x1000: z.array(z.number().int()).nullable(),
  forecast_confidence: z.number().int().nullable(),
  /** **鮮度の判定に使う**（どの観測に基づくか）。 */
  forecast_base_observed_at: z.string().nullable(),
  forecast_model_version: z.string().nullable(),
  /** **水平の起点**（0034）。補間する位置は `in_min ＋（いま − これ）`。 */
  forecast_generated_at: z.string().nullable(),
});

/**
 * `v1_station_neighbors` の 1 行（0039）。
 *
 * **`system_id` と `nb_system_id` は読まない。** `neighborFilters` が同一システムに
 * 絞っているので、どちらも要求した系統と等しいと分かっている。読まない列は
 * `NEIGHBOR_COLUMNS` にも並べない（増えたときに気づける状態を保つ）。
 */
export const neighborRowSchema = z.object({
  station_id: z.string().min(1),
  nb_station_id: z.string().min(1),
  distance_m: z.number().int().nonnegative(),
});

/**
 * `v1_station_hourly` の 1 行（0046）。
 *
 * **`system_id` は読まない**（絞り込みで分かっている）。`NEIGHBOR_COLUMNS` と同じ方針で、
 * 読まない列は並べない。
 */
export const hourlyRowSchema = z.object({
  station_id: z.string().min(1),
  hour_start: z.string(),
  n: z.number().int().positive(),
  bikes_mean: z.number().nonnegative(),
  docks_mean: z.number().nonnegative(),
});

export type FeedRow = z.infer<typeof feedRowSchema>;
export type StationRow = z.infer<typeof stationRowSchema>;
export type NeighborRow = z.infer<typeof neighborRowSchema>;
export type HourlyRow = z.infer<typeof hourlyRowSchema>;
