import { describe, expect, it } from "vitest";
import { WEATHER_BATCH_SIZE } from "./constants";
import { batchCells, truncateToHour, weatherObjectPath } from "./weather-path";

describe("truncateToHour", () => {
  it("時の境界に切り下げる", () => {
    // 2026-09-08T05:17:33Z = 1788844653
    expect(truncateToHour(1_788_844_653)).toBe(1_788_843_600);
  });

  it("ちょうどの時刻は動かさない", () => {
    expect(truncateToHour(1_788_843_600)).toBe(1_788_843_600);
  });

  it("同じ時間内の 2 つの時刻は同じ値になる（取り直しが同じパスに写像される）", () => {
    expect(truncateToHour(1_788_843_601)).toBe(truncateToHour(1_788_847_199));
  });
});

describe("weatherObjectPath", () => {
  it("UTC の年月日と時の epoch と連番で組み立てる", () => {
    expect(weatherObjectPath({ epoch_s: 1_788_844_653, batch: 0 })).toBe(
      "2026/09/08/jma_msm_1788843600_00.json.gz",
    );
  });

  it("連番はゼロ詰めする", () => {
    expect(weatherObjectPath({ epoch_s: 1_788_844_653, batch: 5 })).toContain("_05.json.gz");
  });

  it("同じ時間の再実行は同じパスになる（409 で畳まれる）", () => {
    const first = weatherObjectPath({ epoch_s: 1_788_843_601, batch: 0 });
    const retry = weatherObjectPath({ epoch_s: 1_788_847_100, batch: 0 });
    expect(retry).toBe(first);
  });

  it("日付は UTC で決まる（JST の日付ではない）", () => {
    // 2026-09-08T23:30:00Z は JST では 09-09 の朝。パスは UTC の 09-08
    expect(weatherObjectPath({ epoch_s: 1_788_910_200, batch: 0 })).toContain("2026/09/08/");
  });
});

describe("batchCells", () => {
  it("大きさどおりに分ける", () => {
    expect(batchCells([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
  });

  it("実測の 595 格子は 6 分割になる", () => {
    const cells = Array.from({ length: 595 }, (_, index) => index);
    const batches = batchCells(cells, WEATHER_BATCH_SIZE);
    expect(batches).toHaveLength(6);
    expect(batches.at(-1)).toHaveLength(95);
    expect(batches.flat()).toHaveLength(595);
  });

  it("順序を保つ", () => {
    expect(batchCells(["a", "b", "c"], 2).flat()).toEqual(["a", "b", "c"]);
  });

  it("空なら分割も空", () => {
    expect(batchCells([], 10)).toEqual([]);
  });

  it("大きさ 0 以下は弾く", () => {
    expect(() => batchCells([1], 0)).toThrow();
  });
});
