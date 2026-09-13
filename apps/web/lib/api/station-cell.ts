/**
 * `v1_station_cells` の 1 行を、公開 API の `StationCell` に写す（純粋）。
 *
 * **集約は SQL が済ませてある。** ここがするのは 2 つだけ：
 *   1. 矩形の 4 つの数を 1 つの欄にまとめる
 *   2. **水平ごとの最大を取った曲線を、ポートと同じ補間で 1 点に読む**
 *
 * **補間は `interpolateForecast` を通す。** 地図のポート（`station-row.ts`）と同じ
 * 1 つの実装で、SQL 側には書かない——2 か所に置くと、片方だけ刻みを変えたときに
 * **同じ場所が 2 つの確率を持つ**（W5 プラン §12 の 142 と同じ形）。
 */
import {
  HORIZONS_MIN,
  forecastHorizon,
  interpolateForecast,
  type StationCell,
} from "@bikechance/shared";
import type { CellRow } from "./read-port";

/**
 * セルの確率。**`at` を送っていなければ null**（「いまの確率」は現在値そのもの）。
 *
 * null になる道が 4 つある。どれも「出せない」として同じ形で返す。
 *   1. 到着の指定が無い（`in_min` が null）
 *   2. **そのセルに予測を持つポートが 1 つも無い**（`n_forecast` が 0）
 *   3. 曲線か確度か起点が欠けている
 *   4. 長さがそろっていない（欠けた表から数を作らない）
 *
 * **`horizons_min` は行に入っていない。** SQL が「呼ぶ側の渡した並びと一致する行」
 * だけを集めているので、並びは `HORIZONS_MIN` そのものである——**行ごとに持ち回ると、
 * 一致しているはずのものを毎回確かめ直すことになる**。
 */
const toChance = (
  row: CellRow,
  in_min: number | null,
  now: Date,
): { readonly p_bike: number; readonly p_dock: number; readonly confidence: number } | null => {
  if (in_min === null || row.n_forecast === 0) {
    return null;
  }
  const { p_bike_x1000, p_dock_x1000, confidence, generated_at } = row;
  if (p_bike_x1000 === null || p_dock_x1000 === null) {
    return null;
  }
  if (confidence === null || generated_at === null) {
    return null;
  }
  const horizon_min = forecastHorizon({ in_min, generated_at: new Date(generated_at), now });
  const at = (values: readonly number[]): number | null =>
    interpolateForecast({ horizons_min: HORIZONS_MIN, values_x1000: values, horizon_min });
  const p_bike = at(p_bike_x1000);
  const p_dock = at(p_dock_x1000);
  // **片方だけ返さない**（借りると返すはどちらも表示に要る。`station-row.ts` と同じ規律）
  return p_bike === null || p_dock === null ? null : { p_bike, p_dock, confidence };
};

/** 行を応答の形に写す。 */
export const toCell = (row: CellRow, in_min: number | null, now: Date): StationCell => {
  const chance = toChance(row, in_min, now);
  return {
    cell: { west: row.west, south: row.south, east: row.east, north: row.north },
    n_stations: row.n_stations,
    bikes: row.bikes,
    docks: row.docks,
    stale: row.stale,
    p_bike: chance?.p_bike ?? null,
    p_dock: chance?.p_dock ?? null,
    confidence: chance?.confidence ?? null,
    n_forecast: row.n_forecast,
  };
};
