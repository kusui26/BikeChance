/**
 * 暦の特徴量（開発プラン §6.3・§6.4、W3 プラン §5.6・§9.5）。
 *
 * **暦は「その日が何の日か」だけで決まる純粋な関数にする。** 入力は JST の暦日
 * （`YYYY-MM-DD`）と祝日の集合の 2 つだけで、時計も DB も読まない。学習（Python）と
 * 配信（TypeScript）で同じ答えを出す必要があるので、**同じ規則を 2 つの言語で書き、
 * ゴールデンフィクスチャで突き合わせる**（`fixtures/calendar/day_type_golden.json`）。
 *
 * **`jp_holidays` に入れるのは内閣府 CSV の中身だけ。** 年末年始とお盆は暦の規則で
 * 決まる（12/29–1/3・8/13–16）ので、導けるものを表に持たない。W3 プラン §9.5 は
 * `kind` 列を持つ案だったが、法定の祝日と期間が重なったときにどちらを残すかという
 * 決着のつかない問題が出るため、規則は規則としてここに置く（§12 の 87）。
 */

/** 祝日の集合。`YYYY-MM-DD`（JST の暦日）を持つ。 */
export type HolidaySet = ReadonlySet<string>;

/**
 * 日の種別。**細かいほう**（開発プラン §6.4）。
 * 優先順はこの配列の順ではなく `dayType` の実装で決まる。
 */
export const DAY_TYPES = [
  "newyear", // 年末年始 12/29–1/3
  "obon", // お盆 8/13–16
  "holiday", // 国民の祝日・休日（内閣府 CSV）
  "sun",
  "sat",
  "bridge", // 飛び石：前後が休みの平日
  "weekday",
] as const;
export type DayType = (typeof DAY_TYPES)[number];

/**
 * 曜日の種別。**粗いほう**（開発プラン §6.3 の `dow_type`）。
 *
 * 履歴プロファイルと気候値（B2）のセルはこの 3 値で切る。7 値で切ると
 * 1 セルあたりのサンプルが 1/2 以下になり、`station × dow_type × slot15` が埋まらない。
 */
export const DOW_TYPES = ["weekday", "sat", "sun_holiday"] as const;
export type DowType = (typeof DOW_TYPES)[number];

const MS_PER_DAY = 86_400_000;
const JST_OFFSET_MS = 9 * 3_600_000;

const pad2 = (value: number): string => String(value).padStart(2, "0");

/** `YYYY-MM-DD` を UTC 正午の `Date` にする。**日付の計算はすべてこれを経由する。** */
const toDate = (iso: string): Date => {
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (matched === null) {
    throw new Error(`暦日は YYYY-MM-DD で指定してください: ${iso}`);
  }
  const [, year, month, day] = matched;
  // 正午にするのは、夏時間のある環境で日付が前後しないようにするため
  return new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), 12));
};

const toIso = (at: Date): string =>
  `${at.getUTCFullYear()}-${pad2(at.getUTCMonth() + 1)}-${pad2(at.getUTCDate())}`;

/** `n` 日ずらした暦日。 */
export const shiftDate = (iso: string, days: number): string =>
  toIso(new Date(toDate(iso).getTime() + days * MS_PER_DAY));

/** 時刻から **JST の暦日**を取る。基準時刻のグリッドも学習の暦日も JST（§9.3）。 */
export const jstDate = (at: Date): string => toIso(new Date(at.getTime() + JST_OFFSET_MS));

/** 0 = 日曜。 */
const dayOfWeek = (iso: string): number => toDate(iso).getUTCDay();

/** 年末年始（12/29–1/3）。**暦の規則なので表に持たない。** */
export const isNewYear = (iso: string): boolean => {
  const at = toDate(iso);
  const month = at.getUTCMonth() + 1;
  const day = at.getUTCDate();
  return (month === 12 && day >= 29) || (month === 1 && day <= 3);
};

/** お盆（8/13–16）。**暦の規則なので表に持たない。** */
export const isObon = (iso: string): boolean => {
  const at = toDate(iso);
  return at.getUTCMonth() + 1 === 8 && at.getUTCDate() >= 13 && at.getUTCDate() <= 16;
};

/**
 * 日の種別。**優先順は「期間 → 祝日 → 曜日 → 飛び石」。**
 *
 * 年末年始とお盆を祝日より先に見るのは、期間全体が休業として振る舞うためで、
 * 「1/1 は元日でもあり年末年始でもある」を年末年始に寄せる。
 */
export const dayType = (iso: string, holidays: HolidaySet): DayType => {
  if (isNewYear(iso)) return "newyear";
  if (isObon(iso)) return "obon";
  if (holidays.has(iso)) return "holiday";
  const dow = dayOfWeek(iso);
  if (dow === 0) return "sun";
  if (dow === 6) return "sat";
  return isBridge(iso, holidays) ? "bridge" : "weekday";
};

/** 休みか（土日・祝日・年末年始・お盆）。飛び石の判定に使う。 */
export const isOff = (iso: string, holidays: HolidaySet): boolean => {
  if (isNewYear(iso) || isObon(iso) || holidays.has(iso)) return true;
  const dow = dayOfWeek(iso);
  return dow === 0 || dow === 6;
};

/** 飛び石：**前日も翌日も休みの平日**。挟まれた 1 日は人の動きが休日に寄る。 */
const isBridge = (iso: string, holidays: HolidaySet): boolean =>
  !isOff(iso, holidays) &&
  isOff(shiftDate(iso, -1), holidays) &&
  isOff(shiftDate(iso, 1), holidays);

/** 粗いほうの種別。プロファイルと気候値のセルはこれで切る。 */
export const dowType = (day: DayType): DowType => {
  if (day === "sat") return "sat";
  if (day === "sun" || day === "holiday" || day === "newyear" || day === "obon") {
    return "sun_holiday";
  }
  return "weekday";
};

/** 翌日が休みか。夜の需要が伸びる（§6.3）。 */
export const isDayBeforeHoliday = (iso: string, holidays: HolidaySet): boolean =>
  isOff(shiftDate(iso, 1), holidays);

/**
 * その月の**最後の営業日**か。給与日・締め日の人の動きを拾う（§6.3）。
 *
 * 月末から遡って最初に見つかる非休日。月がまるごと休みということは無いので必ず 1 日ある。
 */
export const isLastBusinessDay = (iso: string, holidays: HolidaySet): boolean => {
  if (isOff(iso, holidays)) return false;
  const at = toDate(iso);
  const lastDay = new Date(Date.UTC(at.getUTCFullYear(), at.getUTCMonth() + 1, 0, 12));
  for (let cursor = lastDay; cursor >= at; cursor = new Date(cursor.getTime() - MS_PER_DAY)) {
    const candidate = toIso(cursor);
    if (!isOff(candidate, holidays)) {
      return candidate === iso;
    }
  }
  return false;
};
