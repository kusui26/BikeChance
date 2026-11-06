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
  COMPACT_CRON,
  INFER_CRON,
  LOAD_WEATHER_CRON,
  ML_MAX_DURATION_S,
  REFERENCE_CRON,
  SYNC_STATIONS_CRON,
  SYSTEMS,
  SYSTEM_IDS,
} from "@bikechance/shared";

const COLLECT_PATH_PREFIX = "/api/jobs/collect/";
const SYNC_PATH_PREFIX = "/api/jobs/sync-stations/";
const WEATHER_PATH = "/api/jobs/archive-weather";
const COMPACT_PATH = "/ml/compact";
const INFER_PATH_PREFIX = "/ml/infer/";
const REFERENCE_PATH = "/ml/reference";
const LOAD_WEATHER_PATH = "/ml/weather";
const MINUTES_PER_HOUR = 60;
/** 推論の格子（分）。`jobs/infer.py` の `GRID_MINUTES` と同じ。 */
const GRID_MINUTES = 5;

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

  it("毎時の Parquet 化（7 分）と時刻の先頭を避ける", () => {
    // 重い処理を同じ分に重ねない。収集は毎分なので避けようがないが、
    // 分の先頭は保守ジョブが動く時間帯と重なりやすい
    const minute = readCrons(WEATHER_PATH)[0]?.schedule.split(" ")[0];
    expect(minute).not.toBe("*");
    expect(minute).not.toBe("0");
    expect(minute).not.toBe("7");
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
    // HELLO は `0-59/5`、ドコモは `1-59/5` で 1 分ずれる。同じ分に置くと、
    // 2 万行の UPSERT が Vercel の同時実行と DB の書き込みで重なる（**分の集合で比べる**）
    const [first, ...rest] = inferMinutes();
    for (const other of rest) {
      expect(first && overlaps(first, other)).toBe(false);
    }
  });

  it("**格子の直後に走る**（5 分毎・格子から 1 分以内に起動）", () => {
    // 推論は基準時刻を 5 分の格子に切り下げ、格子の時刻までに取り込んだ観測だけを使う。
    // 遅れて走るほど同じ予測を遅れて配る。2026-11-03 まで HELLO は `4-59/5` で、
    // 毎周期 1 つ前（5 分前）の観測で予測していた（W9 プランの所見 239・W9-14）
    for (const minutes of inferMinutes()) {
      expect(minutes.size).toBe(MINUTES_PER_HOUR / GRID_MINUTES);
      expect([...minutes].every((minute) => minute % GRID_MINUTES <= 1)).toBe(true);
    }
    expect(INFER_CRON.hellocycling).toBe("0-59/5 * * * *");
    expect(INFER_CRON["docomo-cycle"]).toBe("1-59/5 * * * *");
  });

  it("毎時のジョブと分が重ならない", () => {
    const busy = [WEATHER_PATH, COMPACT_PATH, LOAD_WEATHER_PATH].map(minutesOfPath);
    for (const minutes of inferMinutes()) {
      for (const job of busy) {
        expect(overlaps(minutes, job)).toBe(false);
      }
    }
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

  it("推論と Parquet 化と分が重ならない", () => {
    // ML の仕事は同じインスタンスで重なると rss_mb が跳ねる（W6 プランの所見 212）
    const load = minutesOfPath(LOAD_WEATHER_PATH);
    for (const minutes of [...inferMinutes(), minutesOfPath(COMPACT_PATH)]) {
      expect(overlaps(load, minutes)).toBe(false);
    }
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

  it("毎時のジョブと推論と分が重ならない", () => {
    // 日に 1 回でも、HELLO の推論（0 分起点）と同じ分に置くとインスタンスで重なりうる。
    // 2026-11-03 に 05:00 から 05:02 JST に移した（W9-14）
    const reference = minutesOfPath(REFERENCE_PATH);
    const busy = [WEATHER_PATH, COMPACT_PATH, LOAD_WEATHER_PATH].map(minutesOfPath);
    for (const minutes of [...busy, ...inferMinutes()]) {
      expect(overlaps(reference, minutes)).toBe(false);
    }
  });

  it("属性同期（04:00 JST）と同じ時刻に重ならない", () => {
    const reference = readCrons(REFERENCE_PATH)[0]?.schedule;
    for (const cron of readCrons(SYNC_PATH_PREFIX)) {
      expect(cron.schedule).not.toBe(reference);
    }
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
