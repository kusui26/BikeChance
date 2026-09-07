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
import { SYNC_STATIONS_CRON, SYSTEMS, SYSTEM_IDS } from "@bikechance/shared";

const COLLECT_PATH_PREFIX = "/api/jobs/collect/";
const SYNC_PATH_PREFIX = "/api/jobs/sync-stations/";

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
