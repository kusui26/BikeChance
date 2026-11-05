/**
 * ふだんの傾向を「今日から 7 日 × 24 時間」に組む（W9-05、開発プランの D-47）。純粋。
 *
 * **DB に在るのは 72 個だけ**（曜日種別 × 24 時間。`station_trend`）。中身は日に 1 回しか
 * 変わらないので、168 時間を書き置かず、**ここで暦（祝日）からその日の曜日種別を決めて
 * 24 個を切り出す**（D-08 を直した）。曜日種別の決め方は `@bikechance/shared` の
 * `dayType` / `dowType` で、Python の `features/calendar.py` とゴールデンで突き合わせてある。
 *
 * **予測ではない。** 応答の欄の名前（`trend`）と版の日付（`profile_date`）で、画面が
 * 「ふだんの傾向」と書けるようにする（W9-06）。
 */
import {
  DOW_TYPE_ORDER,
  TREND_DAYS,
  TREND_HOURS_PER_DAY,
  TREND_MAX_AGE_DAYS,
  dayType,
  dowType,
  jstDate,
  shiftDate,
  toNullableProbabilityList,
  type DowType,
  type HolidaySet,
  type StationTrend,
  type TrendDay,
} from "@bikechance/shared";
import type { TrendRow } from "./read-port";

/** 傾向が覆う暦日（JST）。**両端を含む**。祝日を読む範囲もこれで決まる。 */
export const trendWindow = (now: Date): { readonly from: string; readonly to: string } => {
  const today = jstDate(now);
  return { from: today, to: shiftDate(today, TREND_DAYS - 1) };
};

/** 版が古すぎるか（`profile_date` が今日から `TREND_MAX_AGE_DAYS` 日より前）。 */
const isTooOld = (profile_date: string, now: Date): boolean =>
  profile_date < shiftDate(jstDate(now), -TREND_MAX_AGE_DAYS);

/**
 * 72 個の中の添字（`DOW_TYPE_ORDER` の番号 × 24 ＋ 時）。**書く側（Python）と同じ式**で、
 * 2 つの言語の検査が `fixtures/trend/layout_golden.json` に突き合わせる。
 */
export const trendCellIndex = (dow: DowType, hour: number): number =>
  DOW_TYPE_ORDER.indexOf(dow) * TREND_HOURS_PER_DAY + hour;

/** 72 個から、その曜日種別の 0〜23 時の 24 個を切り出す。 */
const hoursOf = (cells: readonly (number | null)[], dow: DowType): readonly (number | null)[] => {
  const start = trendCellIndex(dow, 0);
  return toNullableProbabilityList(cells.slice(start, start + TREND_HOURS_PER_DAY));
};

const toTrendDay = (row: TrendRow, date: string, holidays: HolidaySet): TrendDay => {
  const dow_type = dowType(dayType(date, holidays));
  return {
    date,
    dow_type,
    p_bike: [...hoursOf(row.p_bike_x1000, dow_type)],
    p_dock: [...hoursOf(row.p_dock_x1000, dow_type)],
  };
};

/**
 * 1 ポートの傾向。**行が無い・版が古ければ null**（`forecast_curve` と同じく欄ごとに
 * 落とす。契約 2）。今日の過ぎた時間も返す（W9-05。形を揃え、iOS が薄く描く）。
 */
export const toStationTrend = (params: {
  readonly row: TrendRow | null;
  readonly holidays: HolidaySet;
  readonly now: Date;
}): StationTrend | null => {
  const { row, holidays, now } = params;
  if (row === null || isTooOld(row.profile_date, now)) {
    return null;
  }
  const { from } = trendWindow(now);
  return {
    profile_date: row.profile_date,
    days: Array.from({ length: TREND_DAYS }, (_, offset) =>
      toTrendDay(row, shiftDate(from, offset), holidays),
    ),
  };
};
