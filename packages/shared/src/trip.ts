/**
 * 行程チェックの規則（W4 プラン §6.7、開発プラン §8.3）。
 *
 * **副作用を持たない。** `/v1/trip-check` と、W5 の iOS の Trip Check 画面が同じ規則を
 * 使えるようにここに置く（`forecast.ts` と同じ立ち位置）。
 *
 * 決めてあること（W4-21〜26）：
 *   * **系統は 1 つだけ受ける。** 事業者をまたぐ行程は成立しないので、**表現できなくする**
 *   * **到着は 2 つある。** 出発（借りる）と到着（返す）の**両方**が水平の範囲に入ること
 *   * **`ride_min` を省略したら概算する。** 距離は `rebuild_geo` と同じ haversine
 *   * **`p_trip` は独立仮定。** 注記を数と同じ欄に入れて、切り離せなくする
 */

import { ARRIVAL_MAX_MIN } from "./forecast";

/**
 * 代替候補を探す半径（m）。
 *
 * `station_neighbors` は 500 m まで持っているが（W3 プラン §10.2）、**代替として歩くのは
 * 400 m まで**とする（開発プラン §8.3）。徒歩で約 5 分。
 */
export const TRIP_ALTERNATIVE_RADIUS_M = 400;

/**
 * 1 端点あたりに返す代替候補の上限。
 *
 * 実測（2026-09-11、本番 1,844 ポート）で **400 m 以内の同一システムの近傍は
 * 中央 3・p90 9・最大 25**。全部返すと密集地で応答が 3 倍になる。**中央を丸ごと含み、
 * 密集地では確率の高い順に上から 5 件**を返す（W4-25）。
 */
export const TRIP_ALTERNATIVES_MAX = 5;

/**
 * 乗車時間の概算に使う速度（km/h）。開発プラン §8.3。
 *
 * **直線距離に掛けるので、実際の経路より短く見積もる。** 市街地の経路は直線の
 * 1.2〜1.4 倍あるのが普通で、この概算は**楽観側に外れる**。正確さが要るなら呼ぶ側が
 * `ride_min` を渡す——だから省略可能にしてあり、**サーバーが概算したことは
 * `ride_min_estimated` で明示する**。
 */
export const RIDE_SPEED_KMH = 14;

/**
 * 地球半径（m）。**`rebuild_geo`（migration 0025）と同じ値**にする。
 *
 * 揃えないと、同じ 2 点に SQL と TS が別の距離を出す。`station_neighbors.distance_m` と
 * ここの概算が食い違うと、**400 m で絞ったはずの候補が 410 m に見える**。
 */
export const EARTH_RADIUS_M = 6_371_000;

/**
 * `p_trip` に必ず添える注記（W4-24）。
 *
 * **数と同じ欄に入れる。** 別の場所に置くと、数だけ取り出して注記を落とせてしまう。
 *
 * **ずれる向きは書かない。** 2 つの確率に残る相関は、同じ再配置の車が回ったか、
 * 近くで催しがあったか、といった**モデルが見ていない事情**から来る。正なら実際の成功率は
 * 掛け算より高く、負なら低い——**どちらなのかを測っていない**。測る前に向きを書くと、
 * それ自体が根拠のない断定になる（W6 の shadow 運用で予測ログが貯まれば測れる）。
 */
export const TRIP_INDEPENDENCE_NOTICE =
  "出発ポートで借りられる確率と、到着ポートで返せる確率を、" +
  "互いに影響しないものとみなして掛け合わせた値です。" +
  "同じ天気や時間帯が両方に効くため、実際の成功率はこの値と異なることがあります。";

const MINUTES_PER_HOUR = 60;
const METERS_PER_KM = 1000;

/** 緯度経度の 1 点。`station_attributes` が NULL を返しうるので、呼ぶ側で外す。 */
export type Point = { readonly lat: number; readonly lon: number };

const toRadians = (degrees: number): number => (degrees * Math.PI) / 180;

/**
 * 2 点の大圏距離（m）。**`rebuild_geo` と同じ式・同じ半径**（W4-23）。
 *
 * 距離の実装を 2 つ持たないために、SQL 側の
 * `2 * R * asin(sqrt(sin²(Δφ/2) + cosφ₁·cosφ₂·sin²(Δλ/2)))` をそのまま写す。
 */
export const haversineMeters = (from: Point, to: Point): number => {
  const halfLat = toRadians(to.lat - from.lat) / 2;
  const halfLon = toRadians(to.lon - from.lon) / 2;
  const chord =
    Math.sin(halfLat) ** 2 +
    Math.cos(toRadians(from.lat)) * Math.cos(toRadians(to.lat)) * Math.sin(halfLon) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(chord));
};

/**
 * 乗車時間の概算（分）。**直線距離 ÷ 14 km/h を整数に丸める。**
 *
 * **0 になることがある**（約 116 m 未満）。近いポート同士では正しく、断る理由にならない。
 */
export const estimateRideMinutes = (from: Point, to: Point): number =>
  Math.round((haversineMeters(from, to) / METERS_PER_KM / RIDE_SPEED_KMH) * MINUTES_PER_HOUR);

/** 行程の指定が読めなかった理由。呼ぶ側が Problem の `code` に写す。 */
export type TripProblem = "ride_malformed" | "ride_out_of_range";

export type RideResult =
  | { readonly ok: true; readonly ride_min: number | null }
  | { readonly ok: false; readonly problem: TripProblem; readonly detail: string };

/**
 * `ride_min` として受け付ける形。**10 進の整数だけ**にする。
 *
 * `forecast.ts` の `DECIMAL_MINUTES` と違って小数を許さないのは、**返す値が整数だから**
 * である（`ride_min` は応答にそのまま載る）。小数を受けて黙って丸めると、渡した値と
 * 返る値が違う。
 */
const DECIMAL_INTEGER = /^-?\d+$/;

/**
 * `?ride_min=` を読む。**指定が無ければ null**（呼ぶ側が概算する）。
 *
 * **上限はここで見ない。** 効くのは「出発 ＋ 乗車」が水平に収まるかで、それは
 * `tripArrival` が見る。2 か所で範囲を見ると、片方だけ直したときに食い違う。
 */
export const parseRideMinutes = (raw: string | null): RideResult => {
  if (raw === null || raw.trim() === "") {
    return { ok: true, ride_min: null };
  }
  if (!DECIMAL_INTEGER.test(raw)) {
    return { ok: false, problem: "ride_malformed", detail: "ride_min は整数（分）です。" };
  }
  const ride_min = Number(raw);
  if (ride_min < 0) {
    return {
      ok: false,
      problem: "ride_out_of_range",
      detail: `ride_min は 0 以上です（受け取った値 ${ride_min} 分）。`,
    };
  }
  return { ok: true, ride_min };
};

/** 到着の判定。`forecast.ts` の `ArrivalResult`（出発の読み取り）とは別物なので名前を分ける。 */
export type TripArrivalResult =
  | { readonly ok: true; readonly arrive_in_min: number }
  | { readonly ok: false; readonly detail: string };

/**
 * 到着（返す時刻）を出し、**水平の範囲に収まっているかを見る**（W4-22）。
 *
 * **断らないと黙って嘘になる。** `interpolateForecast` は範囲の外で末尾に張り付くので、
 * 210 分先を頼まれたら「180 分の確率」をそのまま 210 分の答えとして返してしまう。
 * 出発は `parseArrival` が見張っているので、ここは**到着だけ**を見る。
 *
 * 上限しか見ないのは、`depart_in_min` が 5 分以上・`ride_min` が 0 以上だから、
 * 下限は構造的に満たされるためである。
 */
export const tripArrival = (params: {
  readonly depart_in_min: number;
  readonly ride_min: number;
}): TripArrivalResult => {
  const arrive_in_min = params.depart_in_min + params.ride_min;
  if (arrive_in_min > ARRIVAL_MAX_MIN) {
    return {
      ok: false,
      detail:
        `到着（出発 ${params.depart_in_min} 分後 ＋ 乗車 ${params.ride_min} 分 ＝ ` +
        `${arrive_in_min} 分後）が、予測できる範囲（${ARRIVAL_MAX_MIN} 分先まで）を超えています。`,
    };
  }
  return { ok: true, arrive_in_min };
};

/**
 * 行程が成立する確率。**独立とみなして掛ける**（W4-24）。
 *
 * 丸めは `interpolateForecast` と同じ 1/1000 刻みにそろえる。掛けた結果をそのまま返すと
 * 0.8500000000000001 のような値が応答に載る。
 */
export const tripProbability = (p_bike: number, p_dock: number): number =>
  Math.round(p_bike * p_dock * 1000) / 1000;
