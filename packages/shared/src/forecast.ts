/**
 * 到着時刻の指定と、水平からの補間（W4 プラン §4 の W4-01、開発プラン §8.3）。
 *
 * **副作用を持たない。** `/v1` と iOS の両方が同じ規則を使えるようにここに置く。
 *
 * 決めてあること：
 *   * **指定が無ければ予測を返さない。** 「いまの確率」は現在値そのものであって予測では
 *     ない。既定で返さないので、いままでの呼び出しは応答の形も大きさも変わらない
 *   * **`in_min` は 5 分刻みに丸める。** CDN が効くように（`s-maxage=60`）。丸めてから
 *     補間するので、同じ分を指した要求は同じ URL・同じ応答になる
 *   * **持っていない先は作らない。** 水平は最大 180 分で、それより先は 400 で断る。
 *     末尾の値に張り付けて返すと、3 時間先も 5 時間先も同じ数になる
 */

import { HORIZONS_MIN } from "./constants";

/** `in_min` の刻み（分）。5 分に丸める。 */
export const ARRIVAL_STEP_MIN = 5;

/**
 * 受け付ける到着の範囲（分）。**水平の端に合わせる。**
 *
 * 添字ではなく `Math.min` / `Math.max` で取るのは、`HORIZONS_MIN` の並び順に依存させない
 * ためと、`noUncheckedIndexedAccess` の下で `undefined` を混ぜないため。範囲の判定に
 * `undefined` が入ると比較が黙って false になり、検査が効かなくなる。
 */
export const ARRIVAL_MIN_MIN = Math.min(...HORIZONS_MIN);
export const ARRIVAL_MAX_MIN = Math.max(...HORIZONS_MIN);

const MS_PER_MINUTE = 60_000;
const PROBABILITY_SCALE = 1000;

/** `at` にタイムゾーンが付いているか。素の日時を勝手に UTC と決めない。 */
const HAS_TIMEZONE = /(?:Z|[+-]\d{2}:?\d{2})$/;

/**
 * `in_min` として受け付ける形。**10 進の数だけ**にする。
 *
 * `Number()` に任せると `0x10` が 16、`1e2` が 100 として通る。分を表す入力として
 * 意図されたものではなく、受け取ったほうが黙って別の分を返すことになる。
 */
const DECIMAL_MINUTES = /^-?\d+(?:\.\d+)?$/;

/** 到着の指定が読めなかった理由。呼ぶ側が Problem の `code` に写す。 */
export type ArrivalProblem = "arrival_conflict" | "arrival_malformed" | "arrival_out_of_range";

export type ArrivalResult =
  | { readonly ok: true; readonly in_min: number | null }
  | { readonly ok: false; readonly problem: ArrivalProblem; readonly detail: string };

const failed = (problem: ArrivalProblem, detail: string): ArrivalResult => ({
  ok: false,
  problem,
  detail,
});

/** 空文字は「指定なし」と同じに扱う（`?in_min=` だけ付いた URL）。 */
const blankToNull = (value: string | null): string | null =>
  value === null || value.trim() === "" ? null : value;

/** 5 分刻みに丸める。範囲の検査は丸める**前**に行う（`in_min=3` を 5 に化けさせない）。 */
export const roundArrival = (minutes: number): number =>
  Math.round(minutes / ARRIVAL_STEP_MIN) * ARRIVAL_STEP_MIN;

const inRange = (minutes: number): boolean =>
  minutes >= ARRIVAL_MIN_MIN && minutes <= ARRIVAL_MAX_MIN;

const outOfRange = (minutes: number): ArrivalResult =>
  failed(
    "arrival_out_of_range",
    `到着は ${ARRIVAL_MIN_MIN}〜${ARRIVAL_MAX_MIN} 分先までです（受け取った値 ${Math.round(minutes)} 分）。`,
  );

/** 範囲に入っていれば丸めて返す。入口はここ 1 か所にして、検査の抜けを作らない。 */
const accept = (minutes: number): ArrivalResult =>
  inRange(minutes) ? { ok: true, in_min: roundArrival(minutes) } : outOfRange(minutes);

const fromMinutes = (raw: string): ArrivalResult => {
  const minutes = Number(raw);
  return DECIMAL_MINUTES.test(raw) && Number.isFinite(minutes)
    ? accept(minutes)
    : failed("arrival_malformed", "in_min は数（分）です。");
};

const fromTimestamp = (at: string, now: Date): ArrivalResult => {
  const arrival = new Date(at);
  if (Number.isNaN(arrival.getTime())) {
    return failed("arrival_malformed", "at は ISO 8601（タイムゾーン付き）です。");
  }
  // **タイムゾーンを必須にする。** 素の日時を UTC と決めつけると 9 時間ずれる
  if (!HAS_TIMEZONE.test(at)) {
    return failed(
      "arrival_malformed",
      "at にはタイムゾーンを付けてください（例 2026-09-09T14:00:00Z）。",
    );
  }
  return accept((arrival.getTime() - now.getTime()) / MS_PER_MINUTE);
};

/**
 * `?at=` か `?in_min=` を「何分先か」に直す。
 *
 * **両方来たら断る。** どちらを優先するかを決めると、片方を無視したことが呼ぶ側から
 * 見えない。**どちらも無ければ予測を返さない**（`in_min: null`）。
 */
export const parseArrival = (params: {
  readonly at: string | null;
  readonly in_min: string | null;
  readonly now: Date;
}): ArrivalResult => {
  const at = blankToNull(params.at);
  const raw = blankToNull(params.in_min);
  if (at !== null && raw !== null) {
    return failed("arrival_conflict", "at と in_min は同時に指定できません。");
  }
  if (raw !== null) {
    return fromMinutes(raw);
  }
  return at === null ? { ok: true, in_min: null } : fromTimestamp(at, params.now);
};

/** 水平 1 点。`horizons_min` と `p_*_x1000` を組にして、添字のずれを持ち回らない。 */
type Point = { readonly min: number; readonly value_x1000: number };

/**
 * 2 本の配列を点の並びにする。**そろっていなければ null**（欠けた表から数を作らない）。
 *
 * 長さは先に見ているので `filter` は本来何も落とさない。それでも通すのは、型の上で
 * `undefined` を消すためと、DB 側が壊れた行を返したときに静かに 0 として扱わないため。
 */
const toPoints = (
  horizons: readonly number[] | null,
  values: readonly number[] | null,
): readonly Point[] | null => {
  if (horizons === null || values === null || horizons.length === 0) {
    return null;
  }
  if (horizons.length !== values.length) {
    return null;
  }
  const points = horizons
    .map((min, index) => ({ min, value_x1000: values[index] }))
    .filter((point): point is Point => point.value_x1000 !== undefined);
  return points.length === horizons.length ? points : null;
};

const toProbability = (value_x1000: number): number => Math.round(value_x1000) / PROBABILITY_SCALE;

/**
 * `in_min` を挟む 2 点から線形に補間する。呼ぶのは両端の外を除いたあとだけ。
 *
 * 水平が重複していれば割れない。**0 除算で NaN を作らず、手前の値を使う**。
 */
const between = (points: readonly Point[], in_min: number): number | null => {
  const index = points.findIndex((point) => point.min >= in_min);
  const upper = points[index];
  const lower = points[index - 1];
  if (upper === undefined || lower === undefined) {
    return null;
  }
  const span = upper.min - lower.min;
  if (span <= 0) {
    return toProbability(lower.value_x1000);
  }
  const ratio = (in_min - lower.min) / span;
  return toProbability(lower.value_x1000 + ratio * (upper.value_x1000 - lower.value_x1000));
};

/**
 * 水平の並びから `in_min` の確率を線形に補間する。
 *
 * **確率のまま補間する**（logit ではない）。刻みは 5〜60 分で、表示は 10% 単位なので
 * 曲率の影響は表示の分解能に埋もれる。logit で補間すると、`p = 0` や `p = 1` の
 * 端で無限大を扱うことになり、得るものより面倒のほうが大きい。
 *
 * **範囲の外には張り付ける。** `parseArrival` が 5〜180 分に絞っているので、ここに
 * 来るのは端ちょうどの場合だけである。
 *
 * 返すのは **0〜1 の確率**。元が 1/1000 刻みなので、その分解能のまま丸める。
 */
export const interpolateForecast = (params: {
  readonly horizons_min: readonly number[] | null;
  readonly values_x1000: readonly number[] | null;
  readonly in_min: number;
}): number | null => {
  const points = toPoints(params.horizons_min, params.values_x1000);
  if (points === null) {
    return null;
  }
  const first = points[0];
  const last = points[points.length - 1];
  if (first === undefined || last === undefined) {
    return null;
  }
  if (params.in_min <= first.min) {
    return toProbability(first.value_x1000);
  }
  return params.in_min >= last.min
    ? toProbability(last.value_x1000)
    : between(points, params.in_min);
};
