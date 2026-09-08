/**
 * 暦の特徴量（`calendar.ts`、開発プラン §6.3・§6.4）。
 *
 * ここで固定したい契約は 5 つ。
 *   * 年末年始とお盆は**暦の規則**で決まる（表に持たない）。祝日より優先する
 *   * `dow_type` は 3 値。プロファイルと気候値のセルが埋まる粒度にする
 *   * 飛び石は「前後が休みの平日」
 *   * 月末営業日は月末から遡って最初の非休日
 *   * **時刻ではなく JST の暦日**で判断する（UTC で切ると 9 時間ずれる）
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  DAY_TYPES,
  DOW_TYPES,
  type DayType,
  dayType,
  dowType,
  isDayBeforeHoliday,
  isLastBusinessDay,
  isNewYear,
  isObon,
  isOff,
  jstDate,
  shiftDate,
} from "./calendar";

/** 2026 年の内閣府 CSV（18 件）。実データそのまま。 */
const HOLIDAYS_2026 = [
  "2026-01-01", // 元日
  "2026-01-12", // 成人の日
  "2026-02-11", // 建国記念の日
  "2026-02-23", // 天皇誕生日
  "2026-03-20", // 春分の日
  "2026-04-29", // 昭和の日
  "2026-05-03", // 憲法記念日
  "2026-05-04", // みどりの日
  "2026-05-05", // こどもの日
  "2026-05-06", // 休日（5/3 が日曜のための振替）
  "2026-07-20", // 海の日
  "2026-08-11", // 山の日
  "2026-09-21", // 敬老の日
  "2026-09-22", // 休日（国民の休日）
  "2026-09-23", // 秋分の日
  "2026-10-12", // スポーツの日
  "2026-11-03", // 文化の日
  "2026-11-23", // 勤労感謝の日
];
const HOLIDAYS = new Set(HOLIDAYS_2026);

describe("年末年始とお盆は暦の規則", () => {
  it("年末年始は 12/29〜1/3", () => {
    expect(["2026-12-29", "2026-12-31", "2027-01-01", "2027-01-03"].map(isNewYear)).toEqual([
      true,
      true,
      true,
      true,
    ]);
    expect(["2026-12-28", "2027-01-04"].map(isNewYear)).toEqual([false, false]);
  });

  it("お盆は 8/13〜16", () => {
    expect(["2026-08-13", "2026-08-16"].map(isObon)).toEqual([true, true]);
    expect(["2026-08-12", "2026-08-17"].map(isObon)).toEqual([false, false]);
  });

  it("**期間が祝日より優先する**（元日は newyear）", () => {
    // 1/1 は元日（法定の祝日）でもあるが、年末年始の期間全体が休業として振る舞う
    expect(HOLIDAYS.has("2026-01-01")).toBe(true);
    expect(dayType("2026-01-01", HOLIDAYS)).toBe("newyear");
  });
});

describe("day_type", () => {
  it("実データの要点を固定する", () => {
    const cases: readonly [string, string][] = [
      ["2026-01-01", "newyear"], // 木・元日
      ["2026-01-03", "newyear"], // 土
      ["2026-01-04", "sun"], // 日（年末年始を抜けた最初の日曜）
      ["2026-01-05", "weekday"], // 月
      ["2026-01-12", "holiday"], // 月・成人の日
      ["2026-05-06", "holiday"], // 水・振替休日
      ["2026-08-11", "holiday"], // 火・山の日
      ["2026-08-13", "obon"], // 木
      ["2026-08-16", "obon"], // 日（お盆が日曜より優先）
      ["2026-09-22", "holiday"], // 火・国民の休日
      ["2026-12-28", "bridge"], // 月（日曜と年末年始に挟まれた平日）
      ["2026-12-29", "newyear"], // 火
    ];
    for (const [iso, expected] of cases) {
      expect(dayType(iso, HOLIDAYS), iso).toBe(expected);
    }
  });

  it("2026 年の 365 日がすべていずれかの種別になる", () => {
    const counted: Record<string, number> = {};
    for (let iso = "2026-01-01"; iso <= "2026-12-31"; iso = shiftDate(iso, 1)) {
      const type = dayType(iso, HOLIDAYS);
      expect(DAY_TYPES).toContain(type);
      counted[type] = (counted[type] ?? 0) + 1;
    }
    expect(Object.values(counted).reduce((sum, one) => sum + one, 0)).toBe(365);
    // 内閣府 CSV は 18 件だが、元日は newyear に吸われるので holiday は 17
    expect(counted["holiday"]).toBe(17);
    expect(counted["newyear"]).toBe(6);
    expect(counted["obon"]).toBe(4);
    expect(counted["sat"]).toBe(50);
    expect(counted["sun"]).toBe(50);
  });

  it("暦日の形が違えば止まる（静かに別の日を返さない）", () => {
    for (const bad of ["2026/01/01", "2026-1-1", "20260101", "", "きょう"]) {
      expect(() => dayType(bad, HOLIDAYS), bad).toThrow();
    }
  });
});

describe("飛び石", () => {
  it("前後が休みの平日だけ", () => {
    // 2026-08-10（月）は日曜 8/9 と山の日 8/11 に挟まれている
    expect(dayType("2026-08-10", HOLIDAYS)).toBe("bridge");
    // 2026-08-12（水）は山の日 8/11 とお盆 8/13 に挟まれている
    expect(dayType("2026-08-12", HOLIDAYS)).toBe("bridge");
  });

  it("片側だけ休みなら飛び石ではない", () => {
    // 2026-11-04（水）は文化の日 11/3 の翌日だが、翌 11/5 は平日
    expect(dayType("2026-11-04", HOLIDAYS)).toBe("weekday");
  });

  it("土日そのものは飛び石にならない", () => {
    for (let iso = "2026-01-01"; iso <= "2026-12-31"; iso = shiftDate(iso, 1)) {
      if (dayType(iso, HOLIDAYS) === "bridge") {
        expect(isOff(iso, HOLIDAYS), iso).toBe(false);
      }
    }
  });
});

describe("dow_type は 3 値に畳む", () => {
  it("祝日・年末年始・お盆は日曜と同じ扱い", () => {
    const sunLike: readonly DayType[] = ["sun", "holiday", "newyear", "obon"];
    expect(sunLike.map(dowType)).toEqual([
      "sun_holiday",
      "sun_holiday",
      "sun_holiday",
      "sun_holiday",
    ]);
  });

  it("飛び石は平日側（人はおおむね働く）", () => {
    expect(dowType("bridge")).toBe("weekday");
    expect(dowType("weekday")).toBe("weekday");
  });

  it("土曜は独立（需要の形が日曜と違う）", () => {
    expect(dowType("sat")).toBe("sat");
  });

  it("すべての day_type が 3 値のいずれかに写る", () => {
    for (const day of DAY_TYPES) {
      expect(DOW_TYPES, day).toContain(dowType(day));
    }
  });
});

describe("翌日が休みか", () => {
  it("休前日を拾う", () => {
    expect(isDayBeforeHoliday("2026-09-20", HOLIDAYS)).toBe(true); // 翌日が敬老の日
    expect(isDayBeforeHoliday("2026-01-08", HOLIDAYS)).toBe(false); // 翌日は平日の金曜
  });

  it("金曜は常に休前日", () => {
    expect(isDayBeforeHoliday("2026-01-09", HOLIDAYS)).toBe(true); // 金
  });
});

describe("月末営業日", () => {
  it("2026 年の 12 か月すべてで 1 日だけ立つ", () => {
    const days: string[] = [];
    for (let iso = "2026-01-01"; iso <= "2026-12-31"; iso = shiftDate(iso, 1)) {
      if (isLastBusinessDay(iso, HOLIDAYS)) days.push(iso);
    }
    expect(days).toHaveLength(12);
    // 12 月は 12/29〜31 が年末年始なので 12/28（月）が最後の営業日になる
    expect(days.at(-1)).toBe("2026-12-28");
    // 1 月は 1/31 が土曜なので 1/30（金）
    expect(days[0]).toBe("2026-01-30");
  });

  it("休みの日は月末営業日にならない", () => {
    expect(isLastBusinessDay("2026-01-31", HOLIDAYS)).toBe(false); // 土
    expect(isLastBusinessDay("2026-12-31", HOLIDAYS)).toBe(false); // 年末年始
  });
});

describe("JST の暦日", () => {
  it("**UTC で切ると 9 時間ずれる**", () => {
    // UTC の 2026-09-07T15:00Z は JST の 2026-09-08 00:00
    expect(jstDate(new Date("2026-09-07T15:00:00Z"))).toBe("2026-09-08");
    expect(jstDate(new Date("2026-09-07T14:59:59Z"))).toBe("2026-09-07");
  });

  it("JST の 1 日の両端", () => {
    expect(jstDate(new Date("2026-09-08T00:00:00+09:00"))).toBe("2026-09-08");
    expect(jstDate(new Date("2026-09-08T23:59:59+09:00"))).toBe("2026-09-08");
  });
});

describe("日付の足し算", () => {
  it("月と年をまたぐ", () => {
    expect(shiftDate("2026-01-31", 1)).toBe("2026-02-01");
    expect(shiftDate("2026-12-31", 1)).toBe("2027-01-01");
    expect(shiftDate("2027-01-01", -1)).toBe("2026-12-31");
  });

  it("うるう年（2028 年 2 月）", () => {
    expect(shiftDate("2028-02-28", 1)).toBe("2028-02-29");
    expect(shiftDate("2028-02-29", 1)).toBe("2028-03-01");
  });
});

describe("ゴールデンフィクスチャ", () => {
  // **Python 側（学習）が照らすのと同じ表。** ここが落ちるのは、規則を変えたのに
  // `pnpm exec tsx scripts/gen-calendar-golden.ts` を流し直していないとき。
  // 生成し直して**差分をレビューする**（暦の意味が変わったということなので）
  const path = fileURLToPath(
    new URL("../../../fixtures/calendar/day_type_golden.csv", import.meta.url),
  );
  const lines = readFileSync(path, "utf8")
    .split("\n")
    .filter((line) => line !== "" && !line.startsWith("#"));
  const rows = lines.slice(1).map((line) => line.split(","));
  const goldenHolidays = new Set(rows.filter((row) => row[1] !== "").map((row) => row[0] ?? ""));

  it("2 年ぶん（730 日）ある", () => {
    expect(rows).toHaveLength(730);
    expect(rows[0]?.[0]).toBe("2026-01-01");
    expect(rows.at(-1)?.[0]).toBe("2027-12-31");
  });

  it("**実装と 1 日も食い違わない**", () => {
    for (const row of rows) {
      const [iso, , expectedDay, expectedDow, before, lastBusiness] = row;
      if (iso === undefined) continue;
      const day = dayType(iso, goldenHolidays);
      expect(day, iso).toBe(expectedDay);
      expect(dowType(day), iso).toBe(expectedDow);
      expect(isDayBeforeHoliday(iso, goldenHolidays) ? "1" : "0", iso).toBe(before);
      expect(isLastBusinessDay(iso, goldenHolidays) ? "1" : "0", iso).toBe(lastBusiness);
    }
  });

  it("2026 年の祝日はテストの手書きの一覧と一致する", () => {
    const fromGolden = [...goldenHolidays].filter((iso) => iso.startsWith("2026-")).sort();
    expect(fromGolden).toEqual([...HOLIDAYS_2026].sort());
  });
});
