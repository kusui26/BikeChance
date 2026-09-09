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

/** `v1_feeds` から読む列。`select("*")` にしないのは、列が増えたときに気づけるようにするため。 */
export const FEED_COLUMNS =
  "system_id,display_name,expected_cadence_s,poll_interval_s,capacity_is_dynamic,last_observed_at," +
  "forecast_model_version,forecast_generated_at";

export const STATION_COLUMNS =
  "system_id,station_id,name,lat,lon,capacity,bikes,docks,is_installed,is_renting,is_returning," +
  "is_present,last_changed_at," +
  "forecast_horizons_min,forecast_p_bike_x1000,forecast_p_dock_x1000,forecast_confidence," +
  "forecast_base_observed_at,forecast_model_version";

/** 絞り込み 1 つ。`op` は supabase-js の同名メソッドに対応する。 */
export type Filter =
  | { readonly op: "gte" | "lte"; readonly column: string; readonly value: number }
  | { readonly op: "eq"; readonly column: string; readonly value: string };

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
 * `lat` / `lon` を非 null にしているのは、bbox で絞った結果しか読まないからで、
 * 座標の無いポート（属性をまだ取れていない）は範囲比較で自然に外れる（開発プラン §8.3）。
 * 逆に `name` / `capacity` / `bikes` などは NULL があり得る（未取得・未観測）。
 */
export const stationRowSchema = z.object({
  system_id: systemIdSchema,
  station_id: z.string().min(1),
  name: z.string().nullable(),
  lat: z.number(),
  lon: z.number(),
  capacity: z.number().int().nullable(),
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
  forecast_base_observed_at: z.string().nullable(),
  forecast_model_version: z.string().nullable(),
});

export type FeedRow = z.infer<typeof feedRowSchema>;
export type StationRow = z.infer<typeof stationRowSchema>;
