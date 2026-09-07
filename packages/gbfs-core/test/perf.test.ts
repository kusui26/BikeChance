/**
 * 性能テスト（W1 プラン §6.4）。
 *
 * 収集は毎分動くので、CPU 時間がそのまま Vercel の従量課金になる。W1 プラン §4.1 (a) の
 * 実測は HELLO で 69 ms（JSON.parse 22 + Zod 全件検証 42 + 配列組み立て 1 + 直列化 5）。
 *
 * **絶対時間で判定しない。** 壁時計は同じマシンで走る他のテストに引きずられ、CPU 時間でも
 * メモリ帯域の取り合いで 3 倍に膨らむ。実測（2026-09-08）で、単独なら 50 ms のものが
 * 3 プロジェクトを並列に走らせると壁時計 233 ms・CPU 時間 160 ms になり、実装を変えて
 * いないのに落ちた。閾値を上げれば通るが、それでは何を守っているのか分からなくなる。
 *
 * 代わりに **`JSON.parse` だけの時間を同じ実行の中で測り、その何倍かで判定する**。
 * マシンの速さも負荷も両方に等しく効くので比は安定する。守りたいのは
 * 「パースの上に載せた処理が不相応に重くないか」であって、マシンの速さではない。
 */
import { gunzipSync } from "node:zlib";
import { describe, expect, it } from "vitest";
import { buildIngestArgs, normalizeStationStatus, parseStationStatusFeed } from "../src/index";
import { readFeedFixtureBytes } from "./fixtures";

/**
 * `JSON.parse` に対する全工程の倍率の上限。
 * W1 の実測は 69 / 22 ＝ 約 3.1 倍。1 ポートあたりに重い処理を足せばここが伸びる。
 */
const BUDGET_RATIO = 6;

/** 参考として記録する絶対値の目安（判定には使わない）。 */
const REFERENCE_MS = 70;

/** 1 回目は JIT の暖機に使い、2 回目以降の中央値で判定する。 */
const RUNS = 5;

const US_PER_MS = 1000;

const median = (values: readonly number[]): number =>
  [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]!;

/** この工程が使った CPU 時間（ユーザー＋システム、ミリ秒）。 */
const cpuMillisSince = (from: NodeJS.CpuUsage): number => {
  const used = process.cpuUsage(from);
  return (used.user + used.system) / US_PER_MS;
};

/** 与えた処理の CPU 時間を RUNS 回測り、暖機を除いた中央値を返す。 */
const measure = (work: () => void): number => {
  const durations: number[] = [];
  for (let run = 0; run < RUNS; run += 1) {
    const started = process.cpuUsage();
    work();
    durations.push(cpuMillisSince(started));
  }
  return median(durations.slice(1));
};

describe("HELLO の実フィード（14,835 ポート）", () => {
  it(`受信バイト列から RPC 引数までが JSON.parse の ${BUDGET_RATIO} 倍未満`, () => {
    // 保存されているのは gzip なので、収集器と同じ「展開後のバイト列」を先に作っておく
    const raw = gunzipSync(readFeedFixtureBytes("hellocycling", "station_status"));
    const text = raw.toString("utf8");

    // 基準：パースだけ。マシンの速さと負荷はここにも等しく効く
    const parseOnly = measure(() => {
      JSON.parse(text);
    });

    // 本番の収集が通る全工程
    const whole = measure(() => {
      const parsed: unknown = JSON.parse(text);
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
    });

    const ratio = whole / parseOnly;
    // eslint-disable-next-line no-console
    console.log(
      `  CPU 時間: 全工程 ${whole.toFixed(1)} ms / パースのみ ${parseOnly.toFixed(1)} ms` +
        ` = ${ratio.toFixed(2)} 倍（目安 ${REFERENCE_MS} ms・3.1 倍）`,
    );
    expect(ratio).toBeLessThan(BUDGET_RATIO);
  });
});
