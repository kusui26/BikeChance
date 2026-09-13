/**
 * `v1_stations_current` の 1 行を、公開 API の `StationCurrent` に写す（純粋）。
 *
 * **`/v1/stations` と `/v1/trip-check` が同じ実装を通る。** 2 つの経路が同じポートを
 * 別の形で説明する状態を作らない——片方だけ直したときに、地図と行程チェックで
 * 「借りられる確率」が食い違う、という壊れ方をする（CLAUDE.md §2 の原則 4 と同じ考え）。
 *
 * **`in_min` を引数に取る**のがこの分離の要点である。`/v1/stations` は全ポートを同じ
 * 到着時刻で評価するが、`/v1/trip-check` は**出発側と到着側で違う時刻**を渡す
 * （W4 プラン §6.7）。写し方は同じで、時刻だけが違う。
 */
import {
  forecastHorizon,
  interpolateForecast,
  isForecastFresh,
  toProbabilityList,
  type ForecastCurve,
  type RecentHour,
  type StationCurrent,
  type StationForecast,
} from "@bikechance/shared";
import type { HourlyRow, StationRow } from "./read-port";

/**
 * 1 ポートぶんの予測を組み立てる（W4 プラン §4 の W4-01・W4-02）。
 *
 * **null になる道が 4 つある。** どれも「出せない」として同じ形で返し、理由は返さない。
 *   1. 要求に到着の指定が無い（`in_min` が null）
 *   2. そのポートの予測がまだ無い（貸出も返却も止まっている・成果物に無い）
 *   3. **基づく観測が古い**（`FORECAST_STALE_AFTER_S` を超えた）
 *   4. 配列の長さがそろっていない（欠けた表から数を作らない）
 *
 * **3 を `generated_at` ではなく `base_observed_at` で測る**のが要点。利用者に効くのは
 * 「どの観測に基づくか」で、現在値の `stale` 判定と同じ物差しになる。
 *
 * **補間する位置は `in_min` ではない。** 水平の起点は `generated_at` なので、行の年齢を
 * 足した位置で読む（`forecastHorizon`。W4 プラン §12 の 114）。この 2 つを取り違えると、
 * **利用者の到着より早い時刻の確率**を、到着時刻の確率として出すことになる。
 *
 * これで、返す確率が指すのは常に **`response.generated_at ＋ in_min`** になる。
 */
export const toForecast = (
  row: StationRow,
  in_min: number | null,
  now: Date,
): StationForecast | null => {
  if (in_min === null) {
    return null;
  }
  const base = row.forecast_base_observed_at;
  const generated = row.forecast_generated_at;
  if (base === null || generated === null) {
    return null;
  }
  if (row.forecast_confidence === null || row.forecast_model_version === null) {
    return null;
  }
  const base_observed_at = new Date(base);
  if (!isForecastFresh({ base_observed_at, now })) {
    return null;
  }
  return toProbabilities(row, {
    horizon_min: forecastHorizon({ in_min, generated_at: new Date(generated), now }),
    confidence: row.forecast_confidence,
    model_version: row.forecast_model_version,
    base_observed_at,
  });
};

/** 補間して 2 つの確率を出す。**片方だけ返さない**（borrow と return はどちらも表示に要る）。 */
const toProbabilities = (
  row: StationRow,
  found: {
    readonly horizon_min: number;
    readonly confidence: number;
    readonly model_version: string;
    readonly base_observed_at: Date;
  },
): StationForecast | null => {
  const at = (values: readonly number[] | null): number | null =>
    interpolateForecast({
      horizons_min: row.forecast_horizons_min,
      values_x1000: values,
      horizon_min: found.horizon_min,
    });
  const p_bike = at(row.forecast_p_bike_x1000);
  const p_dock = at(row.forecast_p_dock_x1000);
  if (p_bike === null || p_dock === null) {
    return null;
  }
  return {
    p_bike,
    p_dock,
    confidence: found.confidence,
    base_observed_at: found.base_observed_at.toISOString(),
    model_version: found.model_version,
  };
};

/**
 * 座標のそろった行。**`StationCurrent` は座標を必須にしている**ので、写す前に絞る。
 *
 * ビューは座標の無いポートも返しうる（属性が未着・失効）。bbox で引くかぎり範囲比較で
 * 外れるが、**ID で引くと出てくる**——その前提が暗黙だったのを、型で見えるようにした。
 */
export type LocatedRow = StationRow & { readonly lat: number; readonly lon: number };

export const hasLocation = (row: StationRow): row is LocatedRow =>
  row.lat !== null && row.lon !== null;

/**
 * 行を応答の形に写す。
 *
 * `observed_at` に入れるのは**フィードの観測時刻**であって `last_changed_at` ではない。
 * 後者は「最後に値が変わった時刻」で、変わっていないだけの値を古く見せてしまう（§11.3）。
 * ポートが最新のフィードに現れていない（`is_present` が false）なら、その値がいつのものかは
 * 分からないので null にする。
 */
export const toStation = (
  row: LocatedRow,
  observed_at: string | null,
  forecast: StationForecast | null,
): StationCurrent => ({
  system_id: row.system_id,
  station_id: row.station_id,
  name: row.name,
  lat: row.lat,
  lon: row.lon,
  capacity: row.capacity,
  // **`capacity` とは別の数**（0045）。混ぜない——片方は宣言、片方は実測である
  capacity_est: row.capacity_est,
  capacity_days: row.capacity_days,
  bikes: row.bikes,
  docks: row.docks,
  is_installed: row.is_installed,
  is_renting: row.is_renting,
  is_returning: row.is_returning,
  is_present: row.is_present,
  observed_at: row.is_present ? observed_at : null,
  last_changed_at: new Date(row.last_changed_at).toISOString(),
  forecast,
});

/**
 * 予測の曲線を組み立てる（`/v1/stations/{system}/{station_id}`。W5-13）。
 *
 * **補間しない。** `station_forecasts` の生の 10 点をそのまま返す——地図が返すのは
 * 「その到着時刻の 1 点」で、正はどちらも `station_forecasts` にある（契約 4・16）。
 * **同じ確率を 2 つの形で配らない**ための切り分けなので、ここで補間したら意味が消える。
 *
 * **null になる道は `toForecast` と同じ 3 つ**（予測が無い・鮮度切れ・配列が欠けている）。
 * 違うのは「到着の指定が無い」道が無いことだけで、**詳細は `at` を受けない**（曲線を
 * 返すので要らず、受けると同じポートの URL が 5 分ごとに割れて CDN が効かない）。
 *
 * **長さがそろっていなければ返さない。** 欠けた表から数を作らない（`interpolateForecast`
 * が長さを見るのと同じ規律）。
 */
export const toForecastCurve = (row: StationRow, now: Date): ForecastCurve | null => {
  const base = row.forecast_base_observed_at;
  const generated = row.forecast_generated_at;
  const horizons = row.forecast_horizons_min;
  const bike = row.forecast_p_bike_x1000;
  const dock = row.forecast_p_dock_x1000;
  if (base === null || generated === null || row.forecast_model_version === null) {
    return null;
  }
  if (row.forecast_confidence === null || horizons === null || bike === null || dock === null) {
    return null;
  }
  const base_observed_at = new Date(base);
  if (!isForecastFresh({ base_observed_at, now })) {
    return null;
  }
  if (horizons.length === 0 || horizons.length !== bike.length || horizons.length !== dock.length) {
    return null;
  }
  return {
    base_observed_at: base_observed_at.toISOString(),
    generated_at: new Date(generated).toISOString(),
    model_version: row.forecast_model_version,
    confidence: row.forecast_confidence,
    horizons_min: [...horizons],
    p_bike: [...toProbabilityList(bike)],
    p_dock: [...toProbabilityList(dock)],
  };
};

/**
 * 直近の実績を応答の形に写す（0046）。
 *
 * **行を作らない時間帯はそのまま欠ける。** 観測の無かった時間に 0 を入れると
 * 「0 台だった」に見える（CLAUDE.md §6 の「補間しない」と同じ規律）。
 */
export const toRecentHours = (rows: readonly HourlyRow[]): readonly RecentHour[] =>
  rows.map((row) => ({
    hour_start: new Date(row.hour_start).toISOString(),
    n: row.n,
    bikes_mean: row.bikes_mean,
    docks_mean: row.docks_mean,
  }));
