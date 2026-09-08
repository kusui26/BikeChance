import { describe, expect, it } from "vitest";
import {
  GRID_INTERVAL_MIN,
  HORIZONS_MIN,
  JAPAN_BBOX,
  JMA_MSM_COVERAGE_HOURS,
  OPEN_METEO_FORECAST_DAYS,
  OPEN_METEO_MODELS,
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

  it("予報の窓は jma_msm が欠けない範囲に収まる", () => {
    // 窓は JST の当日 0 時から始まるので、覆う時間数は forecast_days × 24。
    // jma_msm が値を返すのは当日 0 時から 76 時間ちょうどで、それを超えると
    // 系列の途中から null になる（W3 プラン §12 の 77）
    expect(OPEN_METEO_FORECAST_DAYS * 24).toBeLessThanOrEqual(JMA_MSM_COVERAGE_HOURS);
    // 広げる意味が無いほど狭くもしない（水平 180 分＋長期側の余裕）
    expect(OPEN_METEO_FORECAST_DAYS).toBeGreaterThanOrEqual(3);
  });

  it("降水確率を持つモデルを必ず含む", () => {
    // precipitation_probability は jma_msm では全件 null で、best_match にしか入らない
    expect(OPEN_METEO_MODELS).toContain("best_match");
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
