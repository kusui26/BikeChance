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
  COMPACT_MAX_DURATION_S,
  SYNC_STATIONS_CRON,
  SYSTEMS,
  SYSTEM_IDS,
} from "@bikechance/shared";

const COLLECT_PATH_PREFIX = "/api/jobs/collect/";
const SYNC_PATH_PREFIX = "/api/jobs/sync-stations/";
const WEATHER_PATH = "/api/jobs/archive-weather";
const COMPACT_PATH = "/ml/compact";

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
    expect(isRecord(first) && first["maxDuration"]).toBe(COMPACT_MAX_DURATION_S);
  });

  it("glob が Python のファイルを指す", () => {
    expect(Object.keys(readMlFunctions())).toEqual(["**/*.py"]);
  });
});
