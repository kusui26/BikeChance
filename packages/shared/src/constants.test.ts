import { describe, expect, it } from "vitest";
import {
  GRID_INTERVAL_MIN,
  HORIZONS_MIN,
  JAPAN_BBOX,
  PROBABILITY_BAND_THRESHOLDS,
  SYSTEMS,
  SYSTEM_IDS,
} from "./constants";

describe("constants", () => {
  it("水平は昇順で、全て 5 分グリッドの倍数", () => {
    const sorted = [...HORIZONS_MIN].sort((a, b) => a - b);
    expect([...HORIZONS_MIN]).toEqual(sorted);
    for (const horizon of HORIZONS_MIN) {
      expect(horizon % GRID_INTERVAL_MIN).toBe(0);
    }
  });

  it("確率の帯が high > medium の順に並ぶ", () => {
    expect(PROBABILITY_BAND_THRESHOLDS.high_min).toBeGreaterThan(
      PROBABILITY_BAND_THRESHOLDS.medium_min,
    );
  });

  it("全システムの定義が揃っている", () => {
    for (const systemId of SYSTEM_IDS) {
      const system = SYSTEMS[systemId];
      expect(system.system_id).toBe(systemId);
      expect(system.operator_name.length).toBeGreaterThan(0);
      expect(system.expected_cadence_s).toBeGreaterThan(0);
    }
  });

  it("日本の BBox が実測したポートの範囲を含む", () => {
    // 実測の最北（稚内周辺 43.11）・最南（沖縄 24.34）・最西（石垣 125.26）・最東（142.16）
    expect(JAPAN_BBOX.lat_min).toBeLessThan(24.34);
    expect(JAPAN_BBOX.lat_max).toBeGreaterThan(43.11);
    expect(JAPAN_BBOX.lon_min).toBeLessThan(125.26);
    expect(JAPAN_BBOX.lon_max).toBeGreaterThan(142.16);
  });
});
