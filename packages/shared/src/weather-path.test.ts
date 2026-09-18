import { describe, expect, it } from "vitest";
import {
  ARCHIVE_WEATHER_MAX_DURATION_S,
  WEATHER_BATCH_SIZE,
  WEATHER_LOCATIONS_PER_MINUTE,
  WEATHER_RATE_WINDOW_MS,
} from "./constants";
import {
  batchCells,
  pacingDelaysMs,
  totalPacingMs,
  truncateToHour,
  weatherObjectPath,
} from "./weather-path";

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

describe("pacingDelaysMs", () => {
  const BUDGET = WEATHER_LOCATIONS_PER_MINUTE;
  const WINDOW = WEATHER_RATE_WINDOW_MS;

  it("予算に収まるうちは待たない", () => {
    expect(pacingDelaysMs([100, 100, 100, 100, 100], BUDGET, WINDOW)).toEqual([0, 0, 0, 0, 0]);
  });

  it("**実測の 602 格子**は 5 分割投げてから 1 度だけ待つ", () => {
    const sizes = batchCells(
      Array.from({ length: 602 }, (_, index) => index),
      WEATHER_BATCH_SIZE,
    ).map((batch) => batch.length);
    expect(sizes).toEqual([100, 100, 100, 100, 100, 100, 2]);
    expect(pacingDelaysMs(sizes, BUDGET, WINDOW)).toEqual([0, 0, 0, 0, 0, WINDOW, 0]);
  });

  it("**どの 1 分の窓も予算を超えない**（そこが守りたいこと）", () => {
    const sizes = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100];
    const delays = pacingDelaysMs(sizes, BUDGET, WINDOW);
    let spent = 0;
    for (const [index, size] of sizes.entries()) {
      if ((delays[index] ?? 0) > 0) {
        spent = 0;
      }
      spent += size;
      expect(spent).toBeLessThanOrEqual(BUDGET);
    }
  });

  it("**待ちは分割の前に置く**（要素数は入力と同じ）", () => {
    expect(pacingDelaysMs([100, 100], BUDGET, WINDOW)).toHaveLength(2);
    expect(pacingDelaysMs([], BUDGET, WINDOW)).toEqual([]);
  });

  it("予算より大きい 1 分割は待たせない（待っても通らない）", () => {
    expect(pacingDelaysMs([600], BUDGET, WINDOW)).toEqual([0]);
  });

  it("予算 0 以下は弾く", () => {
    expect(() => pacingDelaysMs([1], 0, WINDOW)).toThrow();
  });

  it("合計は待ちの和", () => {
    const sizes = [100, 100, 100, 100, 100, 100, 2];
    expect(totalPacingMs(sizes, BUDGET, WINDOW)).toBe(WINDOW);
  });

  it("**いまの格子数なら maxDuration に収まる**（余裕がどれだけあるか）", () => {
    // 602 格子は窓 1 つぶん待つ。取得そのものは実測 5 秒
    const waited = totalPacingMs([100, 100, 100, 100, 100, 100, 2], BUDGET, WINDOW);
    expect(waited + 5_000).toBeLessThan(ARCHIVE_WEATHER_MAX_DURATION_S * 1000);
    // **1,000 格子でもまだ収まる**（窓 1 つ）。1,100 を超えると窓が 2 つになり危うい
    const thousand = Array.from({ length: 10 }, () => 100);
    expect(totalPacingMs(thousand, BUDGET, WINDOW) + 5_000).toBeLessThan(
      ARCHIVE_WEATHER_MAX_DURATION_S * 1000,
    );
  });
});
