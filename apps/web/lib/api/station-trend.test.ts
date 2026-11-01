/**
 * ふだんの傾向の組み立て（`lib/api/station-trend.ts`。W9-05、D-47）。
 *
 * ここで守りたいのは 5 つ。
 *   * **72 個の並びが書く側（Python）と同じ**（`fixtures/trend/layout_golden.json`）
 *   * **その日の曜日種別は暦で決まる**（祝日は `sun_holiday`。土曜の祝日も）
 *   * **今日（JST）から 7 日**。UTC の日付で切らない
 *   * **出せない時間は null のまま**（0 に写さない）
 *   * **古い版は出さない**（`TREND_MAX_AGE_DAYS`）
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  DOW_TYPE_ORDER,
  TREND_HOURS_PER_DAY,
  TREND_MAX_AGE_DAYS,
  stationTrendSchema,
} from "@bikechance/shared";
import { toStationTrend, trendCellIndex, trendWindow } from "./station-trend";
import type { TrendRow } from "./read-port";

/** 2026-09-08（火）09:00 JST。7 日は 09-08〜09-14 で、09-12 が土曜、09-13 が日曜。 */
const NOW = new Date("2026-09-08T00:00:00.000Z");

/** 曜日種別ごとに見分けのつく値：土曜は 100 番台、日祝は 200 番台、平日は 300 番台 ＋ 時。 */
const cells = (): (number | null)[] =>
  DOW_TYPE_ORDER.flatMap((_, dow) =>
    Array.from({ length: TREND_HOURS_PER_DAY }, (_, hour) => (dow + 1) * 100 + hour),
  );

const row = (overrides: Partial<TrendRow> = {}): TrendRow => ({
  profile_date: "2026-09-07",
  p_bike_x1000: cells(),
  p_dock_x1000: cells(),
  ...overrides,
});

const trend = (params: { readonly row?: TrendRow | null; readonly holidays?: string[] } = {}) =>
  toStationTrend({
    row: params.row === undefined ? row() : params.row,
    holidays: new Set(params.holidays ?? []),
    now: NOW,
  });

type Layout = {
  readonly hours_per_day: number;
  readonly dow_type_order: readonly string[];
  readonly cells: readonly {
    readonly dow_type: string;
    readonly hour: number;
    readonly index: number;
  }[];
};

const isLayout = (value: unknown): value is Layout =>
  typeof value === "object" && value !== null && "cells" in value && "dow_type_order" in value;

const readLayout = (): Layout => {
  const path = new URL("../../../../fixtures/trend/layout_golden.json", import.meta.url);
  const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
  if (!isLayout(parsed)) throw new Error("layout_golden.json の形が違う");
  return parsed;
};

describe("72 個の並び（書く側と同じ）", () => {
  it("曜日種別の並びと 1 日の時間の数がフィクスチャと同じ", () => {
    const layout = readLayout();
    expect([...DOW_TYPE_ORDER]).toEqual(layout.dow_type_order);
    expect(TREND_HOURS_PER_DAY).toBe(layout.hours_per_day);
  });

  it("**添字がフィクスチャと同じ**（ずれても例外は出ず、別の曜日の確率が出る）", () => {
    for (const cell of readLayout().cells) {
      const dow = DOW_TYPE_ORDER.find((one) => one === cell.dow_type);
      if (dow === undefined) throw new Error(`知らない曜日種別: ${cell.dow_type}`);
      expect(trendCellIndex(dow, cell.hour), `${cell.dow_type} ${cell.hour} 時`).toBe(cell.index);
    }
  });
});

describe("7 日 × 24 時間", () => {
  it("スキーマを満たす", () => {
    expect(() => stationTrendSchema.parse(trend())).not.toThrow();
  });

  it("**今日（JST）から 7 日**", () => {
    expect(trend()?.days.map((day) => day.date)).toEqual([
      "2026-09-08",
      "2026-09-09",
      "2026-09-10",
      "2026-09-11",
      "2026-09-12",
      "2026-09-13",
      "2026-09-14",
    ]);
  });

  it("**UTC ではなく JST の日付で切る**（00:30 JST はもう次の日）", () => {
    const now = new Date("2026-09-07T15:30:00.000Z");
    expect(trendWindow(now)).toEqual({ from: "2026-09-08", to: "2026-09-14" });
    const days = toStationTrend({ row: row(), holidays: new Set(), now })?.days;
    expect(days?.[0]?.date).toBe("2026-09-08");
  });

  it("曜日種別は暦で決まる（平日・土曜・日曜）", () => {
    expect(trend()?.days.map((day) => day.dow_type)).toEqual([
      "weekday",
      "weekday",
      "weekday",
      "weekday",
      "sat",
      "sun_holiday",
      "weekday",
    ]);
  });

  it("**祝日は sun_holiday**（土曜の祝日も。`dayType` は祝日を土曜より先に見る）", () => {
    const days = trend({ holidays: ["2026-09-10", "2026-09-12"] })?.days;
    expect(days?.[2]?.dow_type).toBe("sun_holiday");
    expect(days?.[4]?.dow_type).toBe("sun_holiday");
  });

  it("**その曜日種別の 24 個を、0 時から順に切り出す**", () => {
    const days = trend()?.days;
    // 平日は 300 番台、土曜は 100 番台、日祝は 200 番台（×1000 → 0〜1）
    expect(days?.[0]?.p_bike[0]).toBe(0.3);
    expect(days?.[0]?.p_bike[23]).toBe(0.323);
    expect(days?.[4]?.p_bike[0]).toBe(0.1);
    expect(days?.[5]?.p_dock[8]).toBe(0.208);
    expect(days?.every((day) => day.p_bike.length === 24 && day.p_dock.length === 24)).toBe(true);
  });

  it("**出せない時間は null のまま**（0% と読める 0 に写さない）", () => {
    const bike = cells();
    bike[trendCellIndex("weekday", 7)] = null;
    const days = trend({ row: row({ p_bike_x1000: bike }) })?.days;
    expect(days?.[0]?.p_bike[7]).toBeNull();
    expect(days?.[0]?.p_bike[8]).toBe(0.308);
    // **土曜の同じ時刻は別の要素**なので欠けない
    expect(days?.[4]?.p_bike[7]).toBe(0.107);
  });

  it("**組が門を通らなかった系統は 24 個とも null**（灰色に描く）", () => {
    const off = Array.from({ length: 72 }, () => null);
    const days = trend({ row: row({ p_bike_x1000: off }) })?.days;
    expect(days?.every((day) => day.p_bike.every((one) => one === null))).toBe(true);
    expect(days?.[0]?.p_dock[0]).toBe(0.3);
  });
});

describe("出さないとき", () => {
  it("行が無ければ null（まだ写していないポート）", () => {
    expect(trend({ row: null })).toBeNull();
  });

  it("**古い版は出さない**（境目の日は出す）", () => {
    // NOW の JST の今日は 09-08。`TREND_MAX_AGE_DAYS` 日前（09-01）までは出す
    expect(TREND_MAX_AGE_DAYS).toBe(7);
    expect(trend({ row: row({ profile_date: "2026-09-01" }) })?.profile_date).toBe("2026-09-01");
    expect(trend({ row: row({ profile_date: "2026-08-31" }) })).toBeNull();
  });
});
