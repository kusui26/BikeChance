/**
 * 暦のゴールデンフィクスチャを作る（W3 プラン §5.6）。
 *
 * **暦の規則は TypeScript（配信）と Python（学習）の 2 つの言語で書く。** 同じ答えを
 * 出すことを機械で守るために、`packages/shared` の実装から表を吐き、両方のテストが
 * それに照らす。**TypeScript を変えたらフィクスチャの差分が出る**ので、レビューで
 * 「暦の意味が変わった」ことに気づける（CLAUDE.md §3 のゴールデンテスト）。
 *
 * 祝日そのものもフィクスチャに入れる。こうすると Python 側のテストは DB も CSV も
 * 要らず、この 1 ファイルだけで完結する。
 *
 * 範囲を 2026-01-01〜2027-12-31 にするのは、内閣府 CSV の収録が 2027-11-23 までで、
 * それ以降は法定の祝日が無いぶん規則だけで決まるため。両端の判定（飛び石・休前日）に
 * 要る前後 1 日は、どちらも年末年始で規則から決まるので範囲の外を見なくてよい。
 *
 * 使い方:
 *   pnpm exec tsx scripts/gen-calendar-golden.ts
 */
import { writeFileSync } from "node:fs";
import {
  dayType,
  dowType,
  isDayBeforeHoliday,
  isLastBusinessDay,
  shiftDate,
} from "@bikechance/shared";

const CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv";
const OUT = "fixtures/calendar/day_type_golden.csv";
const FROM = "2026-01-01";
const TO = "2027-12-31";

/** 内閣府の CSV は **Shift_JIS**。UTF-8 として読むと名称が化ける（日付だけは読めるので気づきにくい）。 */
const parseCsv = (body: Uint8Array): ReadonlyMap<string, string> => {
  const text = new TextDecoder("shift_jis").decode(body);
  const rows = new Map<string, string>();
  for (const line of text.split(/\r?\n/).slice(1)) {
    if (line.trim() === "") continue;
    const [date, name] = line.split(",");
    if (date === undefined || name === undefined) continue;
    const [year, month, day] = date.split("/");
    if (year === undefined || month === undefined || day === undefined) continue;
    rows.set(`${year}-${month.padStart(2, "0")}-${day.padStart(2, "0")}`, name.trim());
  }
  return rows;
};

const main = async (): Promise<void> => {
  const received = await fetch(CSV_URL, {
    headers: { "User-Agent": "BikeChance/0.1 (calendar golden)" },
  });
  if (received.status !== 200) {
    throw new Error(`内閣府 CSV が ${received.status} を返しました`);
  }
  const lastModified = received.headers.get("last-modified") ?? "(不明)";
  const holidays = parseCsv(new Uint8Array(await received.arrayBuffer()));
  const holidaySet = new Set(holidays.keys());

  const lines = [
    "# 暦のゴールデンフィクスチャ（W3 プラン §5.6）",
    "# 生成: pnpm exec tsx scripts/gen-calendar-golden.ts",
    "# 規則の正: packages/shared/src/calendar.ts",
    `# 祝日の出典: 内閣府「国民の祝日について」 last-modified: ${lastModified}`,
    "# **手で編集しない。** 規則を変えたら生成し直し、差分をレビューする",
    "date,holiday_name,day_type,dow_type,is_day_before_holiday,is_last_business_day",
  ];
  let count = 0;
  for (let iso = FROM; iso <= TO; iso = shiftDate(iso, 1)) {
    const day = dayType(iso, holidaySet);
    lines.push(
      [
        iso,
        holidays.get(iso) ?? "",
        day,
        dowType(day),
        isDayBeforeHoliday(iso, holidaySet) ? "1" : "0",
        isLastBusinessDay(iso, holidaySet) ? "1" : "0",
      ].join(","),
    );
    count += 1;
  }
  writeFileSync(OUT, `${lines.join("\n")}\n`, "utf8");
  console.log(`${OUT} に ${count} 日ぶんを書きました（${FROM} 〜 ${TO}）`);
  console.log(`祝日の出典 last-modified: ${lastModified}`);
};

await main();
