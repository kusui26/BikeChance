/**
 * 低ズームで返すもの（ポートか、格子のセルか）の決め方（W5 プラン §6.5、W5-12）。
 *
 * **純粋な関数だけを置く。** サーバーが何を返したかは応答の `aggregation` に書くが、
 * **決め方の正はここ 1 つ**である——Web（W10）が「どちらが返るか」を先読みするときも、
 * サーバーが実際に切り替えるときも、同じ表を通る。
 *
 * **`zoom` は受けない。矩形の辺で決める**（W5-12）。クライアントがズーム段を送らなくても
 * 壊れず、`zoom` を受けると**同じ矩形に 2 つの答え**ができて CDN の鍵が割れる。
 *
 * **刻みは `BBOX_QUANTUM_DEG` の倍数**にする。別の丸めを持ち込むと、セルの境界と
 * `quantizeBbox` が返す実効矩形がずれる。
 */

import { BBOX_MAX_SPAN_DEG, BBOX_QUANTUM_DEG, bboxSpans, type Bbox } from "./bbox";

/**
 * **丸めた後の長辺**がここまでなら**ポートをそのまま返す**（度。サーバーは `quantizeBbox` の後で比べる）。
 *
 * 実測（2026-09-13、本番）で東京駅中心 0.10 度は **1,091 ポート**で上限（1,000）を超え、
 * 0.05 度は 295 ポートで収まる。**0.08 度**は両者の間にとった。
 *
 * **iOS はこれを超える矩形を要求しない**（`Bbox.requestMaxSpanDegrees`。W6 の契約 41）。
 * **同じ値を両側の検査が書いている**ので、変えるときは両方を同時に直す。以前ここには
 * 「iOS は 0.1 度より広い矩形を要求しないので、アプリの挙動は変わらない」と書いていたが、
 * **比べる向きが逆だった**——iOS は 0.08〜0.1 度（丸めると最大約 0.12 度）を要求し、
 * セルが返ると読めずに失敗の帯を出していた（W6 プランの所見 194）。
 */
export const CELL_STATION_MAX_SPAN_DEG = 0.08;

/**
 * 度を格子（`BBOX_QUANTUM_DEG`）の数にする。**辺の比べ方は必ずこれを通す**（W6 プランの所見 205）。
 *
 * 丸めた矩形でも、引き算の結果は格子の倍数ちょうどにならない——経度 139.84 − 139.76 は
 * 0.0800000000000125 になり、度のまま `<= 0.08` で比べると**ちょうど 8 格子の矩形がセルに
 * 落ちる**（経度 139 度台の 8 格子の矩形の 56%）。`quantizeBbox` と同じく 9 桁で端数を落とす。
 */
const inQuanta = (degrees: number): number => Number((degrees / BBOX_QUANTUM_DEG).toFixed(9));

/** 辺が上限以下か。**格子の数で比べる**（端数で境目がずれないように）。 */
const isWithin = (span: number, limit: number): boolean => inQuanta(span) <= inQuanta(limit);

/** 1 段ぶんの決まり。**辺がここまでなら、この刻み**。 */
export type CellStep = {
  readonly max_span_deg: number;
  readonly cell_deg: number;
};

/**
 * セルの刻みの段（辺の短い順）。
 *
 * 格子の数の上限は **25 × 25 ＝ 625**（0.25 度 ÷ 0.01 度）と **10 × 10 ＝ 100**
 * （0.5 度 ÷ 0.05 度）。**値の入るセルはこれよりずっと少ない**——0.01 度セルは全国で
 * 5,587 個しかない（§13.6）。
 */
export const CELL_STEPS: readonly CellStep[] = [
  { max_span_deg: 0.25, cell_deg: BBOX_QUANTUM_DEG },
  { max_span_deg: BBOX_MAX_SPAN_DEG, cell_deg: BBOX_QUANTUM_DEG * 5 },
];

/** 何を返すか。**同じ応答に両方は入れない**（W5-12）。 */
export type CellPlan =
  { readonly aggregation: "station" } | { readonly aggregation: "cell"; readonly cell_deg: number };

/**
 * 矩形の**長いほうの辺**で決める。
 *
 * 短いほうではなく長いほうを見るのは、**細長い矩形でも件数は長辺で決まる**から。
 * 東西に細長い 0.5 × 0.02 度は、緯度の幅が狭くても経度方向に 55 km あり、
 * ポートの数は 0.5 度の矩形に近い。
 *
 * **最後の段に収まらなければ、いちばん粗い刻み**を使う。`parseBbox` が
 * `BBOX_MAX_SPAN_DEG` で断っているのでここには来ないが、**上限の判定を 2 か所に
 * 分けない**ために、こちらでも落ちない形にしておく。
 */
export const planCells = (bbox: Bbox): CellPlan => {
  const spans = bboxSpans(bbox);
  const span = Math.max(spans.lat, spans.lon);
  if (isWithin(span, CELL_STATION_MAX_SPAN_DEG)) {
    return { aggregation: "station" };
  }
  const step = CELL_STEPS.find((one) => isWithin(span, one.max_span_deg)) ?? CELL_STEPS.at(-1);
  // `CELL_STEPS` は空にしない（上の `at(-1)` が undefined になるのはそのときだけ）
  return step === undefined
    ? { aggregation: "station" }
    : { aggregation: "cell", cell_deg: step.cell_deg };
};
