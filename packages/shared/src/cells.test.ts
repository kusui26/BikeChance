import { describe, expect, it } from "vitest";
import { BBOX_MAX_SPAN_DEG, BBOX_QUANTUM_DEG, quantizeBbox, type Bbox } from "./bbox";
import { CELL_STATION_MAX_SPAN_DEG, CELL_STEPS, planCells } from "./cells";

/** 中心を東京駅に置いた正方形。**辺だけが効く**ので、中心はどこでもよい。 */
const square = (span: number): Bbox => ({
  west: 139.767 - span / 2,
  south: 35.681 - span / 2,
  east: 139.767 + span / 2,
  north: 35.681 + span / 2,
});

describe("planCells", () => {
  it("**狭い矩形はポートのまま**（いままでの応答を変えない）", () => {
    expect(planCells(square(0.02))).toEqual({ aggregation: "station" });
    expect(planCells(square(0.05))).toEqual({ aggregation: "station" });
  });

  it("**境目ちょうどはポート**（0.08 度）", () => {
    expect(CELL_STATION_MAX_SPAN_DEG).toBe(0.08);
    expect(planCells(square(CELL_STATION_MAX_SPAN_DEG))).toEqual({ aggregation: "station" });
  });

  it("**境目を越えたらセル**（0.10 度で 1,091 ポートになる矩形）", () => {
    expect(planCells(square(0.1))).toEqual({ aggregation: "cell", cell_deg: 0.01 });
  });

  it("**広い矩形は粗い刻み**（0.5 度）", () => {
    expect(planCells(square(BBOX_MAX_SPAN_DEG))).toEqual({ aggregation: "cell", cell_deg: 0.05 });
  });

  it("刻みの境目は 0.25 度（そこまでは細かいまま）", () => {
    expect(planCells(square(0.25))).toEqual({ aggregation: "cell", cell_deg: 0.01 });
    expect(planCells(square(0.2501))).toEqual({ aggregation: "cell", cell_deg: 0.05 });
  });

  it("**長いほうの辺で決める**（細長い矩形でも件数は長辺で決まる）", () => {
    // 緯度 0.02 度・経度 0.5 度。**南北は狭いが東西に 45 km ある**
    const flat: Bbox = { west: 139.5, south: 35.67, east: 140.0, north: 35.69 };
    expect(planCells(flat)).toEqual({ aggregation: "cell", cell_deg: 0.05 });
    const tall: Bbox = { west: 139.76, south: 35.5, east: 139.78, north: 36.0 };
    expect(planCells(tall)).toEqual({ aggregation: "cell", cell_deg: 0.05 });
  });

  it("**刻みは `BBOX_QUANTUM_DEG` の倍数**（別の丸めを持ち込まない）", () => {
    for (const step of CELL_STEPS) {
      const ratio = step.cell_deg / BBOX_QUANTUM_DEG;
      expect(Number.isInteger(Number(ratio.toFixed(9)))).toBe(true);
    }
  });

  it("段は辺の短い順で、最後は矩形の上限に届く", () => {
    const spans = CELL_STEPS.map((step) => step.max_span_deg);
    expect(spans).toEqual([...spans].sort((left, right) => left - right));
    expect(spans.at(-1)).toBe(BBOX_MAX_SPAN_DEG);
  });

  it("**格子の数は上限の内側**（0.25 度 ÷ 0.01 度 ＝ 25 × 25 ＝ 625）", () => {
    for (const step of CELL_STEPS) {
      const side = Math.ceil(Number((step.max_span_deg / step.cell_deg).toFixed(9)));
      expect(side * side).toBeLessThanOrEqual(625);
    }
  });

  it("**丸めた後の矩形で決める**（丸めで辺が伸びても破綻しない）", () => {
    // 0.079 度は境目の内側だが、格子に丸めると 0.09 度になりセルへ移る
    const requested = square(0.079);
    expect(planCells(requested)).toEqual({ aggregation: "station" });
    expect(planCells(quantizeBbox(requested))).toEqual({ aggregation: "cell", cell_deg: 0.01 });
  });
});
