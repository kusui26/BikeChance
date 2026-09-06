/**
 * 性能テスト（W1 プラン §6.4）。
 *
 * 収集は毎分動くので、CPU 時間がそのまま Vercel の従量課金になる。W1 プラン §4.1 (a) の
 * 実測は HELLO で 69 ms（JSON.parse 22 + Zod 全件検証 42 + 配列組み立て 1 + 直列化 5）。
 * 上限を 150 ms に置き、倍以上遅くなったら気づけるようにする。
 *
 * CI のマシンは開発機より遅いので、閾値には余裕を持たせてある。ここが落ちたときに
 * 疑うべきは「マシンが遅い」ではなく「1 ポートごとに重い処理を足した」の方。
 */
import { gunzipSync } from "node:zlib";
import { describe, expect, it } from "vitest";
import { buildIngestArgs, normalizeStationStatus, parseStationStatusFeed } from "../src/index";
import { readFeedFixtureBytes } from "./fixtures";

/** 検証＋正規化＋配列組み立て＋直列化の合計。開発機の実測は約 69 ms。 */
const BUDGET_MS = 150;

/** 1 回目は JIT の暖機に使い、2 回目以降の中央値で判定する。 */
const RUNS = 5;

const median = (values: readonly number[]): number =>
  [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]!;

describe("HELLO の実フィード（14,835 ポート）", () => {
  it(`受信バイト列から RPC 引数までが ${BUDGET_MS} ms 未満`, () => {
    // 保存されているのは gzip なので、収集器と同じ「展開後のバイト列」を先に作っておく
    const raw = gunzipSync(readFeedFixtureBytes("hellocycling", "station_status"));

    const durations: number[] = [];
    for (let run = 0; run < RUNS; run += 1) {
      const started = performance.now();
      const parsed: unknown = JSON.parse(raw.toString("utf8"));
      const result = parseStationStatusFeed(parsed);
      if (!result.ok) throw new Error(result.issues.join());
      const args = buildIngestArgs({
        system_id: "hellocycling",
        feed: normalizeStationStatus(result.feed),
        fetched_at: new Date(),
        etag: null,
        raw_path: "p",
      });
      JSON.stringify(args);
      durations.push(performance.now() - started);
    }

    const elapsed = median(durations.slice(1));
    // eslint-disable-next-line no-console
    console.log(`  検証＋正規化＋組み立て＋直列化: 中央値 ${elapsed.toFixed(1)} ms`);
    expect(elapsed).toBeLessThan(BUDGET_MS);
  });
});
