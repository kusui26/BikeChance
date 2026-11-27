/**
 * `vercel.json` の Cron 設定が、共有定数の `poll_interval_s` と食い違わないようにする。
 *
 * 監視の閾値は DB の `app_config.collect_interval_s` から導いている（W1-29）。
 * Cron だけを速くして設定を戻し忘れると、「収集は毎分なのにウォッチドッグは 10 分
 * 待つ」という、**壊れていないように見えて監視だけが緩い**状態に黙ってなる。
 * ここで Cron 式と定数を突き合わせておけば、片方だけの変更が CI で目に付く。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  ARCHIVE_WEATHER_CRON,
  ARCHIVE_WEATHER_MAX_DURATION_S,
  COMPACT_CRON,
  INFER_CRON,
  LOAD_WEATHER_CRON,
  ML_MAX_DURATION_S,
  REFERENCE_CRON,
  SYNC_STATIONS_CRON,
  SYSTEMS,
  SYSTEM_IDS,
  WEATHER_CATCH_UP_CRON,
} from "@bikechance/shared";

const COLLECT_PATH_PREFIX = "/api/jobs/collect/";
const SYNC_PATH_PREFIX = "/api/jobs/sync-stations/";
const WEATHER_PATH = "/api/jobs/archive-weather";
const WEATHER_CATCH_UP_PATH = "/api/jobs/weather-catch-up";
const COMPACT_PATH = "/ml/compact";
const INFER_PATH_PREFIX = "/ml/infer/";
const REFERENCE_PATH = "/ml/reference";
const LOAD_WEATHER_PATH = "/ml/weather";
const ML_PATH_PREFIX = "/ml/";
const HELLO_INFER_PATH = `${INFER_PATH_PREFIX}hellocycling`;
const DOCOMO_INFER_PATH = `${INFER_PATH_PREFIX}docomo-cycle`;
const MINUTES_PER_HOUR = 60;
const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = MINUTES_PER_HOUR * SECONDS_PER_MINUTE;
/** 推論の格子（分）。`jobs/infer.py` の `GRID_MINUTES` と同じ。 */
const GRID_MINUTES = 5;

/** 分の中で起動する秒の範囲 [早い, 遅い]。 */
type StartSeconds = readonly [number, number];

/**
 * **起動の秒をあてにしない**ときの範囲（分の中のどの秒にもなりうる）。Vercel Cron の分の中の
 * 起動の秒は、仕事ごとに決まっていて毎日ほぼ同じだが、**スケジュールを変えると変わる**
 * （W11 プランの所見 246）。
 */
const ANY_SECOND: StartSeconds = [0, SECONDS_PER_MINUTE];

/** 仕事が走りうる時間：分の中の起動の秒と、最長の所要（秒）。 */
type Timing = {
  readonly start_s: StartSeconds;
  readonly longest_s: number;
};

type MlJob = Timing & {
  readonly path: string;
  /**
   * `start_s` を測ったときのスケジュール。**測った秒は、このスケジュールでだけ正しい。**
   * 測っていなければ null（`ANY_SECOND` か、重ならないための条件を置く）。
   */
  readonly measured_on: string | null;
};

/**
 * 参照（05:04）の起動が、分の何秒までなら 05:05 の HELLO の前に終わるか（W12 プランの W12-05）。
 * 参照の最長 54 秒と HELLO の起動 45 秒から（51 ＋ 54 ＝ 60 ＋ 45）。**まだ測っていない**
 * ——スケジュールを変えたので、マージの翌朝に測り、過ぎていたら分を選び直す（W12 プラン §4.4）。
 */
const REFERENCE_START_BOUND_S = 51;

/**
 * **ML のサービスの Cron と、それぞれが走りうる時間**（W11 プランの W11-01・所見 246、
 * W12 プランの W12-05）。
 *
 * ML のサービスは 1 つの関数で、同時に呼ばれると同じインスタンスで走りうる——重なると
 * `rss_mb` が積み上がる（W6 プランの所見 212、W10 プランの所見 244）。だから**起動の分ではなく、
 * 走りうる秒の区間で比べる**（2026-11-26 までは分で比べ、ドコモを 1 分とみていた。ドコモは
 * 2 分 44〜48 秒に起動して 3 分台まで走る——所見 246）。
 *
 *   * 推論と Parquet 化の起動の秒は、2026-11-17（W11 の PR P）からの実測。最長の所要は実測を
 *     切り上げた値（ほかの仕事と重なっていた回を除く。HELLO 70.5 秒・ドコモ 35.7 秒・Parquet 化
 *     6.8 秒・天気の取り込み 24.1 秒・参照 53.7 秒。2026-11-22 に測った）
 *   * **天気の取り込み（29 分）と参照（05:04）は、2026-11-26 にスケジュールを変えたので、秒を
 *     まだ測っていない**。天気の取り込みはどの秒に呼ばれても重ならない。参照は
 *     `REFERENCE_START_BOUND_S` 秒までに起動すれば重ならない
 */
const ML_JOBS: readonly MlJob[] = [
  { path: HELLO_INFER_PATH, measured_on: "0-59/5 * * * *", start_s: [45, 49], longest_s: 71 },
  { path: DOCOMO_INFER_PATH, measured_on: "2-59/5 * * * *", start_s: [44, 48], longest_s: 36 },
  { path: COMPACT_PATH, measured_on: "9 * * * *", start_s: [24, 28], longest_s: 7 },
  { path: LOAD_WEATHER_PATH, measured_on: null, start_s: ANY_SECOND, longest_s: 25 },
  { path: REFERENCE_PATH, measured_on: null, start_s: [0, REFERENCE_START_BOUND_S], longest_s: 54 },
];

const jobOf = (path: string): MlJob => {
  const job = ML_JOBS.find((one) => one.path === path);
  if (job === undefined) throw new Error(`${path} は ML_JOBS にありません`);
  return job;
};

/**
 * HELLO の推論が、起動した分から**走りうる分の数**（格子の 49 秒後までに呼ばれ、最長 71 秒で
 * 格子の 120 秒後に終わる）。ドコモはこの直後の分に起動する。
 */
const HELLO_RUN_MINUTES = Math.ceil(
  (jobOf(HELLO_INFER_PATH).start_s[1] + jobOf(HELLO_INFER_PATH).longest_s) / SECONDS_PER_MINUTE,
);

type CronEntry = {
  readonly path: string;
  readonly schedule: string;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

const isCronEntry = (value: unknown): value is CronEntry =>
  isRecord(value) && typeof value["path"] === "string" && typeof value["schedule"] === "string";

/** Vercel Cron は UTC・分粒度で、秒のフィールドを持たない。 */
const everyNMinutes = (interval_s: number): string => {
  const minutes = interval_s / 60;
  if (!Number.isInteger(minutes) || minutes < 1) {
    throw new Error(`${interval_s} 秒は分の Cron 式にできません`);
  }
  return minutes === 1 ? "* * * * *" : `*/${minutes} * * * *`;
};

const readCrons = (prefix: string): readonly CronEntry[] => {
  const file = fileURLToPath(new URL("../../vercel.json", import.meta.url));
  const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
  if (!isRecord(parsed) || !Array.isArray(parsed["crons"])) {
    throw new Error("vercel.json に crons の配列がありません");
  }
  const entries: readonly unknown[] = parsed["crons"];
  return entries.filter(isCronEntry).filter((cron) => cron.path.startsWith(prefix));
};

const readCollectCrons = (): readonly CronEntry[] => readCrons(COLLECT_PATH_PREFIX);

/** `a-b`・`a`・`*` を [下端, 上端] にする。**`a/n` は `a-59/n` と同じ**（Cron の慣わし）。 */
const minuteRange = (range: string, stepped: boolean): readonly [number, number] => {
  if (range === "*") return [0, MINUTES_PER_HOUR - 1];
  const [low, high] = range.split("-").map(Number);
  if (low === undefined || Number.isNaN(low)) {
    throw new Error(`分のフィールドを読めません: ${range}`);
  }
  return [low, high ?? (stepped ? MINUTES_PER_HOUR - 1 : low)];
};

/**
 * Cron 式の分のフィールドを、**その時に走る分の集合**にする（`*`・`*\/n`・`a-b/n`・`a`・`a,b`）。
 * **式の文字列どうしを比べない**——`0-59/5` と `25` は違う文字列だが、毎時 25 分に重なる。
 */
const minutesOf = (schedule: string): ReadonlySet<number> => {
  const field = schedule.split(" ")[0] ?? "";
  const minutes = field.split(",").flatMap((part) => {
    const [range = "", step_text] = part.split("/");
    const step = step_text === undefined ? 1 : Number(step_text);
    const [low, high] = minuteRange(range, step_text !== undefined);
    return Array.from({ length: Math.floor((high - low) / step) + 1 }, (_, i) => low + i * step);
  });
  return new Set(minutes);
};

const overlaps = (a: ReadonlySet<number>, b: ReadonlySet<number>): boolean =>
  [...a].some((minute) => b.has(minute));

const minutesOfPath = (path: string): ReadonlySet<number> =>
  minutesOf(readCrons(path)[0]?.schedule ?? "");

const inferMinutes = (): readonly ReadonlySet<number>[] =>
  readCrons(INFER_PATH_PREFIX).map((cron) => minutesOf(cron.schedule));

/** 走りうる時間（時の頭からの秒。[始め, 終わり]）。 */
type Interval = readonly [number, number];

/** 起動する分ごとに、**分の中の起動の秒から、最も遅い起動 ＋ 最長の所要まで**を走りうるとみる。 */
const runIntervals = (schedule: string, timing: Timing): readonly Interval[] =>
  [...minutesOf(schedule)].map((minute) => [
    minute * SECONDS_PER_MINUTE + timing.start_s[0],
    minute * SECONDS_PER_MINUTE + timing.start_s[1] + timing.longest_s,
  ]);

/** 2 つの区間が重なるか。**時をまたぐ**（59 分に起動して 0 分にかかる仕事）。端が接するだけは重ならない。 */
const intervalsOverlap = (a: Interval, b: Interval): boolean =>
  [-SECONDS_PER_HOUR, 0, SECONDS_PER_HOUR].some(
    (shift) => a[0] < b[1] + shift && b[0] + shift < a[1],
  );

/** 2 つの仕事が走りうる時間が重なるか。**日に 1 回の仕事も、毎時その分に起動するとみる**（時は見ない）。 */
const timingsOverlap = (
  schedule_a: string,
  timing_a: Timing,
  schedule_b: string,
  timing_b: Timing,
): boolean =>
  runIntervals(schedule_a, timing_a).some((a) =>
    runIntervals(schedule_b, timing_b).some((b) => intervalsOverlap(a, b)),
  );

const scheduleOf = (path: string): string => readCrons(path)[0]?.schedule ?? "";

describe("分の集合の読み方（下の検査が使う）", () => {
  it("刻み・範囲・単独の分を開く", () => {
    expect([...minutesOf("4-59/5 * * * *")]).toEqual([
      4, 9, 14, 19, 24, 29, 34, 39, 44, 49, 54, 59,
    ]);
    expect([...minutesOf("*/5 * * * *")]).toEqual([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]);
    expect([...minutesOf("0-59/5 * * * *")]).toEqual([...minutesOf("*/5 * * * *")]);
    expect([...minutesOf("17 * * * *")]).toEqual([17]);
    expect([...minutesOf("2 20 * * *")]).toEqual([2]);
    expect([...minutesOf("7,37 * * * *")]).toEqual([7, 37]);
  });

  it("**文字列が違っても同じ分は重なりとして数える**", () => {
    expect(overlaps(minutesOf("0-59/5 * * * *"), minutesOf("25 * * * *"))).toBe(true);
    expect(overlaps(minutesOf("1-59/5 * * * *"), minutesOf("28 * * * *"))).toBe(false);
  });

  it("走りうる時間は、起動する分ごとに「最も早い起動」から「最も遅い起動 ＋ 最長の所要」まで", () => {
    const hello: Timing = { start_s: [45, 49], longest_s: 71 };
    expect(runIntervals("0-59/5 * * * *", hello).slice(0, 2)).toEqual([
      [45, 120],
      [345, 420],
    ]);
    expect(runIntervals("4 20 * * *", { start_s: ANY_SECOND, longest_s: 30 })).toEqual([
      [240, 330],
    ]);
  });

  it("**分が違っても、走りうる時間が重なれば重なりとして数える**（時をまたぐ）", () => {
    // 2026-11-17 までのドコモ（1 分起点。1 分 11 秒ごろに起動）は、HELLO の長い周期と重なった
    // （W10 プランの所見 244）。2 分起点なら、どの秒に呼ばれても重ならない
    const hello: Timing = { start_s: [45, 49], longest_s: 71 };
    const docomo: Timing = { start_s: ANY_SECOND, longest_s: 36 };
    expect(timingsOverlap("0-59/5 * * * *", hello, "1-59/5 * * * *", docomo)).toBe(true);
    expect(timingsOverlap("0-59/5 * * * *", hello, "2-59/5 * * * *", docomo)).toBe(false);
    // 59 分に起動して長引く仕事は、次の時の 0 分の仕事にかかる
    const late: Timing = { start_s: [50, 50], longest_s: 60 };
    expect(timingsOverlap("59 * * * *", late, "0-59/5 * * * *", hello)).toBe(true);
    // 端が接するだけ（120 秒に終わり、120 秒に始まる）は重ならない
    expect(intervalsOverlap([45, 120], [120, 156])).toBe(false);
  });
});

describe("vercel.json の収集 Cron", () => {
  it("システムごとにちょうど 1 本ある", () => {
    const paths = readCollectCrons().map((cron) => cron.path);
    expect([...paths].sort()).toEqual(
      [...SYSTEM_IDS].map((id) => `${COLLECT_PATH_PREFIX}${id}`).sort(),
    );
  });

  it("スケジュールが poll_interval_s と一致する", () => {
    for (const cron of readCollectCrons()) {
      const system_id = cron.path.slice(COLLECT_PATH_PREFIX.length);
      const system = SYSTEM_IDS.find((id) => id === system_id);
      expect(system, `${cron.path} が未知のシステムを指しています`).toBeDefined();
      if (system === undefined) continue;
      expect(cron.schedule).toBe(everyNMinutes(SYSTEMS[system].poll_interval_s));
    }
  });

  it("Cron 式への変換が期待どおり", () => {
    expect(everyNMinutes(60)).toBe("* * * * *");
    expect(everyNMinutes(300)).toBe("*/5 * * * *");
    expect(() => everyNMinutes(30)).toThrow();
  });
});

describe("vercel.json の属性同期 Cron", () => {
  it("システムごとにちょうど 1 本ある", () => {
    const paths = readCrons(SYNC_PATH_PREFIX).map((cron) => cron.path);
    expect([...paths].sort()).toEqual(
      [...SYSTEM_IDS].map((id) => `${SYNC_PATH_PREFIX}${id}`).sort(),
    );
  });

  it("スケジュールが共有定数と一致する（04:00 JST ＝ 19:00 UTC）", () => {
    for (const cron of readCrons(SYNC_PATH_PREFIX)) {
      expect(cron.schedule).toBe(SYNC_STATIONS_CRON);
    }
  });

  it("収集の毎分と時刻が重ならない", () => {
    // 毎分の収集が走っている最中に 7.8 MB の同期を始めると、
    // ODPT への同時要求と Vercel の同時実行が重なる。分をずらしておく
    const minutes = readCrons(SYNC_PATH_PREFIX).map((cron) => cron.schedule.split(" ")[0]);
    expect(minutes.every((minute) => minute !== "*")).toBe(true);
  });
});

describe("vercel.json の天気アーカイブ Cron", () => {
  it("ちょうど 1 本ある", () => {
    expect(readCrons(WEATHER_PATH)).toHaveLength(1);
  });

  it("スケジュールが共有定数と一致する", () => {
    expect(readCrons(WEATHER_PATH)[0]?.schedule).toBe(ARCHIVE_WEATHER_CRON);
  });

  it("毎時の Parquet 化と時刻の先頭を避ける", () => {
    // 重い処理を同じ分に重ねない。収集は毎分なので避けようがないが、
    // 分の先頭は保守ジョブが動く時間帯と重なりやすい
    const minute = readCrons(WEATHER_PATH)[0]?.schedule.split(" ")[0];
    expect(minute).not.toBe("*");
    expect(minute).not.toBe("0");
    expect(overlaps(minutesOfPath(WEATHER_PATH), minutesOfPath(COMPACT_PATH))).toBe(false);
  });
});

describe("vercel.json の天気の取り直し Cron（W12 の PR T）", () => {
  it("ちょうど 1 本あり、スケジュールが共有定数と一致する", () => {
    const crons = readCrons(WEATHER_CATCH_UP_PATH);
    expect(crons).toHaveLength(1);
    expect(crons[0]?.schedule).toBe(WEATHER_CATCH_UP_CRON);
  });

  it("**毎時 1 回、定時の回が時間の上限で必ず終わった後の、同じ時間のうちに走る**", () => {
    // 発行の時刻は時に丸める。同じ時間のうちでなければ、取り直す相手（その時間の発行）が変わる。
    // 定時の回（17 分）が時間の上限（5 分）まで走っても、取り直しの回はその後に始まる
    const [archive] = [...minutesOfPath(WEATHER_PATH)];
    const catch_up = [...minutesOfPath(WEATHER_CATCH_UP_PATH)];
    expect(catch_up).toHaveLength(1);
    const [minute] = catch_up;
    const archive_ends = (archive ?? MINUTES_PER_HOUR) + ARCHIVE_WEATHER_MAX_DURATION_S / 60;
    expect(minute).toBeGreaterThan(archive_ends);
    expect(minute).toBeLessThan(MINUTES_PER_HOUR);
  });

  it("**取り直しの回も時間の上限で終わり、その時間のうちに収まる**（次の時の定時の回と重ならない）", () => {
    const [minute] = [...minutesOfPath(WEATHER_CATCH_UP_PATH)];
    const ends = (minute ?? MINUTES_PER_HOUR) + ARCHIVE_WEATHER_MAX_DURATION_S / 60;
    expect(ends).toBeLessThanOrEqual(MINUTES_PER_HOUR);
  });

  it("ML のサービスの道ではない（推論とインスタンスを共有しない）", () => {
    expect(WEATHER_CATCH_UP_PATH.startsWith(ML_PATH_PREFIX)).toBe(false);
    expect(readCrons(ML_PATH_PREFIX).map((cron) => cron.path)).not.toContain(WEATHER_CATCH_UP_PATH);
  });
});

describe("vercel.json の Parquet 化 Cron", () => {
  it("ちょうど 1 本ある", () => {
    expect(readCrons(COMPACT_PATH)).toHaveLength(1);
  });

  it("スケジュールが共有定数と一致する", () => {
    expect(readCrons(COMPACT_PATH)[0]?.schedule).toBe(COMPACT_CRON);
  });

  it("前 1 時間を畳むので、時刻の先頭から十分に離す", () => {
    // ドコモの停滞閾値は最大 4 分強（W1-42）。その時間帯の観測が入り終わる前に
    // 畳むと、後から行が増えて 2 回目と食い違う
    const minute = Number(readCrons(COMPACT_PATH)[0]?.schedule.split(" ")[0]);
    expect(minute).toBeGreaterThanOrEqual(5);
  });

  it("天気アーカイブと同じ分に重ならない", () => {
    const weather = readCrons(WEATHER_PATH)[0]?.schedule.split(" ")[0];
    const compact = readCrons(COMPACT_PATH)[0]?.schedule.split(" ")[0];
    expect(compact).not.toBe(weather);
  });
});

describe("vercel.json の推論 Cron", () => {
  it("システムごとにちょうど 1 本ある", () => {
    // **段階的に入れた（W3-19）。** 20,745 行 × 288 回/日 ＝ 597 万行更新/日になるので、
    // 配列列の HOT 更新が効くかを測ってから広げる約束だった。HELLO だけを 11 時間 5 分・
    // 134 サイクル動かし、失敗 0・HOT 率 100.0%・`n_dead_tup` が 1 周期ぶんで頭打ちに
    // なることを確かめてからドコモを足した（W3 プラン §5.10・§7.8）。
    //
    // 以降は収集・属性同期と同じ「全システムに 1 本ずつ」を不変条件にする。
    // **システムを増やして推論だけ忘れる**のを、ここで止める
    const paths = readCrons(INFER_PATH_PREFIX).map((cron) => cron.path);
    expect([...paths].sort()).toEqual(
      [...SYSTEM_IDS].map((id) => `${INFER_PATH_PREFIX}${id}`).sort(),
    );
  });

  it("スケジュールが共有定数と一致する", () => {
    for (const cron of readCrons(INFER_PATH_PREFIX)) {
      const system_id = cron.path.slice(INFER_PATH_PREFIX.length);
      const system = SYSTEM_IDS.find((id) => id === system_id);
      if (system === undefined) continue;
      expect(cron.schedule).toBe(INFER_CRON[system]);
    }
  });

  it("2 系統が同じ分に重ならない", () => {
    // HELLO は `0-59/5`、ドコモは `2-59/5` で 2 分ずれる。同じ分に置くと、
    // 2 万行の UPSERT が Vercel の同時実行と DB の書き込みで重なる（**分の集合で比べる**）
    const [first, ...rest] = inferMinutes();
    for (const other of rest) {
      expect(first && overlaps(first, other)).toBe(false);
    }
  });

  it("**HELLO は格子の直後、ドコモは HELLO が走りうる分の直後に起動する**", () => {
    // 推論は基準時刻を 5 分の格子に切り下げ、格子の時刻までに取り込んだ観測だけを使う。
    // 遅れて走るほど同じ予測を遅れて配る。2026-11-03 まで HELLO は `4-59/5` で、
    // 毎周期 1 つ前（5 分前）の観測で予測していた（W9 プランの所見 239・W9-14）。
    // ドコモは HELLO の周期が終わるまで待ち（W10 プランの所見 244）、それより遅らせない
    const offsets = (path: string): ReadonlySet<number> =>
      new Set([...minutesOfPath(path)].map((minute) => minute % GRID_MINUTES));
    for (const minutes of inferMinutes()) {
      expect(minutes.size).toBe(MINUTES_PER_HOUR / GRID_MINUTES);
    }
    expect(offsets(HELLO_INFER_PATH)).toEqual(new Set([0]));
    expect(offsets(DOCOMO_INFER_PATH)).toEqual(new Set([HELLO_RUN_MINUTES]));
    expect(INFER_CRON.hellocycling).toBe("0-59/5 * * * *");
    expect(INFER_CRON["docomo-cycle"]).toBe("2-59/5 * * * *");
  });
});

describe("vercel.json の天気の取り込み Cron", () => {
  it("ちょうど 1 本ある", () => {
    expect(readCrons(LOAD_WEATHER_PATH)).toHaveLength(1);
  });

  it("スケジュールが共有定数と一致する", () => {
    expect(readCrons(LOAD_WEATHER_PATH)[0]?.schedule).toBe(LOAD_WEATHER_CRON);
  });

  it("アーカイブ（毎時 17 分）の後に走る", () => {
    // 取り込むのはアーカイブが置いた発行。同じ時の中で後ろに置く
    const [archive] = [...minutesOfPath(WEATHER_PATH)];
    const [load] = [...minutesOfPath(LOAD_WEATHER_PATH)];
    expect(load).toBeGreaterThan(archive ?? MINUTES_PER_HOUR);
  });
});

describe("vercel.json の参照スナップショット Cron", () => {
  it("ちょうど 1 本ある", () => {
    expect(readCrons(REFERENCE_PATH)).toHaveLength(1);
  });

  it("スケジュールが共有定数と一致する", () => {
    expect(readCrons(REFERENCE_PATH)[0]?.schedule).toBe(REFERENCE_CRON);
  });

  it("rebuild_geo（pg_cron 19:30 UTC）より後に走る", () => {
    // 近傍と行政区画がその日ぶん更新された状態を固めたい。**順番が意味を持つ**
    const [minute, hour] = (readCrons(REFERENCE_PATH)[0]?.schedule ?? "").split(" ");
    expect(Number(hour) * 60 + Number(minute)).toBeGreaterThan(19 * 60 + 30);
  });

  it("属性同期（04:00 JST）と同じ時刻に重ならない", () => {
    const reference = readCrons(REFERENCE_PATH)[0]?.schedule;
    for (const cron of readCrons(SYNC_PATH_PREFIX)) {
      expect(cron.schedule).not.toBe(reference);
    }
  });
});

describe("vercel.json の ML のサービスの仕事（推論・Parquet 化・天気の取り込み・参照）", () => {
  it("ML のサービスの Cron は、走りうる時間を決めたものだけ", () => {
    // ML の Cron を足すときに、走りうる時間（`ML_JOBS`）を決めずに済ませない
    const paths = readCrons(ML_PATH_PREFIX).map((cron) => cron.path);
    expect([...paths].sort()).toEqual(ML_JOBS.map((job) => job.path).sort());
  });

  it("**起動の秒を測った仕事は、測ったときのスケジュールのまま**", () => {
    // スケジュールを変えると、分の中の起動の秒も変わる（W11 プランの所見 246）。変えたら
    // `measured_on` を null にして秒をあてにしない形で確かめ、マージの後に測り直す
    for (const job of ML_JOBS) {
      if (job.measured_on === null) continue;
      expect(scheduleOf(job.path), job.path).toBe(job.measured_on);
    }
  });

  it("**走りうる時間が重ならない**（起動の分が違うだけでは足りない）", () => {
    // 2026-11-17 まではドコモ（1 分起点）が HELLO（0 分起点）と別の分だったのに、HELLO が
    // 長い周期で 1 分台に食い込み、直近 7 日の 5.3% の周期で重なった（W10 プランの所見 244）。
    // 日に 1 回の参照も、毎時の仕事と同じ時間に置けば重なりうる（時は見ない）。
    // 天気アーカイブは Web のサービスの仕事で、インスタンスを共有しないので数えない
    const pairs = ML_JOBS.flatMap((a, i) => ML_JOBS.slice(i + 1).map((b) => ({ a, b })));
    expect(pairs).toHaveLength((ML_JOBS.length * (ML_JOBS.length - 1)) / 2);
    for (const { a, b } of pairs) {
      expect(
        timingsOverlap(scheduleOf(a.path), a, scheduleOf(b.path), b),
        `${a.path} と ${b.path}`,
      ).toBe(false);
    }
  });

  it("**推論のほかの ML の仕事は、毎 5 分の 4 分台に置く**（推論の合間。W12 プランの W12-05）", () => {
    // ドコモの最も遅い終わり（3 分 24 秒）の後、次の HELLO の起動（5 分 45 秒）の前
    for (const path of [COMPACT_PATH, LOAD_WEATHER_PATH, REFERENCE_PATH]) {
      const offsets = [...minutesOfPath(path)].map((minute) => minute % GRID_MINUTES);
      expect(offsets, path).toEqual([GRID_MINUTES - 1]);
    }
  });

  it("**2026-11-26 までの天気の取り込み（28 分）と参照（05:03）は、ドコモの長い回と重なりうる**", () => {
    // 28 分 23 秒・05:03 の 16 秒に起動していた（実測）。ドコモが分の後半に呼ばれて最長まで
    // 長引くと、その終わり（3 分 24 秒）にかかる（W11 プランの所見 246）
    const docomo = jobOf(DOCOMO_INFER_PATH);
    const docomo_schedule = INFER_CRON["docomo-cycle"];
    const old_weather: Timing = { start_s: [23, 27], longest_s: 25 };
    const old_reference: Timing = { start_s: [16, 17], longest_s: 54 };
    expect(timingsOverlap(docomo_schedule, docomo, "28 * * * *", old_weather)).toBe(true);
    expect(timingsOverlap(docomo_schedule, docomo, "3 20 * * *", old_reference)).toBe(true);
    expect(
      timingsOverlap(docomo_schedule, docomo, LOAD_WEATHER_CRON, jobOf(LOAD_WEATHER_PATH)),
    ).toBe(false);
    expect(timingsOverlap(docomo_schedule, docomo, REFERENCE_CRON, jobOf(REFERENCE_PATH))).toBe(
      false,
    );
  });
});

describe("vercel.json の ml サービス", () => {
  /** services.<name>.functions は glob → 設定。glob は**サービスの root からの相対**。 */
  const readMlFunctions = (): Record<string, unknown> => {
    const file = fileURLToPath(new URL("../../vercel.json", import.meta.url));
    const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
    if (!isRecord(parsed) || !isRecord(parsed["services"])) {
      throw new Error("vercel.json に services がありません");
    }
    const ml: unknown = parsed["services"]["ml"];
    if (!isRecord(ml) || !isRecord(ml["functions"])) {
      throw new Error("services.ml に functions がありません");
    }
    return ml["functions"];
  };

  it("maxDuration が共有定数と一致する", () => {
    // Python には route segment config が無い。maxDuration の指定はここだけ
    const settings = Object.values(readMlFunctions());
    expect(settings).toHaveLength(1);
    const first: unknown = settings[0];
    expect(isRecord(first) && first["maxDuration"]).toBe(ML_MAX_DURATION_S);
  });

  it("glob が Python のファイルを指す", () => {
    expect(Object.keys(readMlFunctions())).toEqual(["**/*.py"]);
  });

  it("Pro の上限（300 秒）を超えない", () => {
    // glob は `**/*.py` の 1 本しかないので、`/ml/compact` と `/ml/infer/*` は
    // **同じ上限を共有する**。だから定数も 1 つしかない（ML_MAX_DURATION_S）。
    // 上限を超えるとデプロイが拒否される
    expect(ML_MAX_DURATION_S).toBeLessThanOrEqual(300);
    // shadow（active と shadow の両方で LightGBM を歩く）が約 144 秒（W4 プラン §6.8）
    expect(ML_MAX_DURATION_S).toBeGreaterThan(144);
  });
});
