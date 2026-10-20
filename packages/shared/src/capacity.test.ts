import { describe, expect, it } from "vitest";
import { isCapacityUnbounded, type CapacityFigures } from "./capacity";
import { UNBOUNDED_CAPACITY_MIN } from "./constants";

const none: CapacityFigures = { capacity: null, docks: null, capacity_est: null };

describe("isCapacityUnbounded（W7-05）", () => {
  it.each<[string, CapacityFigures]>([
    // 2026-10-20 の本番の値
    [
      "HELLO の 20038（宣言の容量 1000・空き枠 991）",
      { capacity: 1000, docks: 991, capacity_est: 1000 },
    ],
    [
      "ドコモの 4161（空き枠 32767。容量は動的なので null）",
      { capacity: null, docks: 32767, capacity_est: 0 },
    ],
    ["ドコモの 5753（推定容量 9999 だけ）", { capacity: null, docks: null, capacity_est: 9999 }],
  ])("**番兵のポートは上限なし**：%s", (_label, figures) => {
    expect(isCapacityUnbounded(figures)).toBe(true);
  });

  it.each<[string, CapacityFigures]>([
    ["ふつうの HELLO（宣言の容量 116 が最大）", { capacity: 116, docks: 40, capacity_est: 116 }],
    [
      "ふつうのドコモ（空き枠 93・推定容量 126 が最大）",
      { capacity: null, docks: 93, capacity_est: 126 },
    ],
    ["何も分からない", none],
  ])("ふつうのポートは上限なしではない：%s", (_label, figures) => {
    expect(isCapacityUnbounded(figures)).toBe(false);
  });

  it("境目：1000 で上限なし、999 では上限なしではない（3 つのどれでも同じ）", () => {
    for (const key of ["capacity", "docks", "capacity_est"] as const) {
      expect(isCapacityUnbounded({ ...none, [key]: UNBOUNDED_CAPACITY_MIN })).toBe(true);
      expect(isCapacityUnbounded({ ...none, [key]: UNBOUNDED_CAPACITY_MIN - 1 })).toBe(false);
    }
  });
});
