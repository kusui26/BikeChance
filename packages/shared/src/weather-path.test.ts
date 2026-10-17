import { describe, expect, it } from "vitest";
import {
  ARCHIVE_WEATHER_MAX_DURATION_S,
  WEATHER_BATCH_SIZE,
  WEATHER_DB_TIMEOUT_MS,
  WEATHER_FETCH_TIMEOUT_MS,
  WEATHER_LOCATIONS_PER_MINUTE,
  WEATHER_RATE_WINDOW_MS,
  WEATHER_RECORD_MARGIN_MS,
} from "./constants";
import {
  WEATHER_BATCH_WORST_MS,
  batchCells,
  fitsArchiveBudget,
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

describe("時間の上限と、投げ始めてよいかの判断（W6-38・W7-01、W6 プランの所見 220・223）", () => {
  /** 2026-10-16 の発行のセル数（7 分割）。 */
  const CURRENT_GRID_CELLS = 617;
  const MAX_MS = ARCHIVE_WEATHER_MAX_DURATION_S * 1000;
  /** 分割を投げる前に経っている時間の最悪：格子の取得と `job_started`（どちらも DB の時間切れまで）。 */
  const BEFORE_FIRST_BATCH_MS = 2 * WEATHER_DB_TIMEOUT_MS;

  const sizesOf = (cells: number): number[] =>
    batchCells(
      Array.from({ length: cells }, (_, index) => index),
      WEATHER_BATCH_SIZE,
    ).map((batch) => batch.length);

  /** **分割のどれもが時間切れになった**ときの取得の所要（保存なし）：待ち ＋ 分割の数 × 時間切れ。 */
  const fetchOnlyWorstMs = (cells: number): number => {
    const sizes = sizesOf(cells);
    const waited = totalPacingMs(sizes, WEATHER_LOCATIONS_PER_MINUTE, WEATHER_RATE_WINDOW_MS);
    return waited + sizes.length * WEATHER_FETCH_TIMEOUT_MS;
  };

  /** **どの分割も最悪の所要**（取得も保存も時間切れまで）のときに、判断に従って投げていく。 */
  const simulateWorst = (cells: number): { sent: number; skipped: number; elapsed_ms: number } => {
    const sizes = sizesOf(cells);
    const delays = pacingDelaysMs(sizes, WEATHER_LOCATIONS_PER_MINUTE, WEATHER_RATE_WINDOW_MS);
    return delays.reduce(
      (state, delay) =>
        fitsArchiveBudget(state.elapsed_ms, delay)
          ? {
              ...state,
              sent: state.sent + 1,
              elapsed_ms: state.elapsed_ms + delay + WEATHER_BATCH_WORST_MS,
            }
          : { ...state, skipped: state.skipped + 1 },
      { sent: 0, skipped: 0, elapsed_ms: BEFORE_FIRST_BATCH_MS },
    );
  };

  it("**120 秒では、いまの格子数の最悪を越えていた**（PR P の前。所見 223）", () => {
    expect(fetchOnlyWorstMs(CURRENT_GRID_CELLS)).toBe(170_000);
    expect(fetchOnlyWorstMs(CURRENT_GRID_CELLS)).toBeGreaterThan(120_000);
  });

  it("**いまの格子数なら、取得も保存も最悪でも 7 分割とも投げられる**", () => {
    const run = simulateWorst(CURRENT_GRID_CELLS);
    expect(run.sent).toBe(7);
    expect(run.skipped).toBe(0);
    expect(run.elapsed_ms + WEATHER_RECORD_MARGIN_MS).toBeLessThanOrEqual(MAX_MS);
  });

  it.each([1_000, 2_000])(
    "**格子が増えても上限は越えない**——投げられない分割は投げずに残す（%i 格子）",
    (cells) => {
      const run = simulateWorst(cells);
      expect(run.skipped).toBeGreaterThan(0);
      expect(run.sent + run.skipped).toBe(sizesOf(cells).length);
      expect(run.elapsed_ms + WEATHER_RECORD_MARGIN_MS).toBeLessThanOrEqual(MAX_MS);
    },
  );

  it("境目：記録の余裕までちょうど使い切る分割は投げ、1 ミリ秒でも越える分割は投げない", () => {
    const edge = MAX_MS - WEATHER_RECORD_MARGIN_MS - WEATHER_BATCH_WORST_MS;
    expect(fitsArchiveBudget(edge, 0)).toBe(true);
    expect(fitsArchiveBudget(edge + 1, 0)).toBe(false);
    expect(fitsArchiveBudget(edge - WEATHER_RATE_WINDOW_MS, WEATHER_RATE_WINDOW_MS)).toBe(true);
  });
});
