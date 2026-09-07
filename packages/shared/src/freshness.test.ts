import { describe, expect, it } from "vitest";
import { SYSTEMS } from "./constants";
import { STALE_CADENCE_MULTIPLIER, isStale, staleAfterSeconds } from "./freshness";

const NOW = new Date("2026-09-08T00:00:00.000Z");
const ago = (seconds: number): Date => new Date(NOW.getTime() - seconds * 1000);

describe("staleAfterSeconds", () => {
  it("監視（0013）の式と一致する", () => {
    // greatest(expected * 3, poll * 3) + poll
    expect(staleAfterSeconds({ expected_cadence_s: 300, poll_interval_s: 60 })).toBe(960);
    expect(staleAfterSeconds({ expected_cadence_s: 81, poll_interval_s: 60 })).toBe(303);
  });

  it("取得間隔のほうが長いフィードでも破綻しない", () => {
    expect(staleAfterSeconds({ expected_cadence_s: 30, poll_interval_s: 300 })).toBe(1200);
  });

  it("実測の最悪値を超える余裕がある", () => {
    // W1-42：ドコモの観測遅れは 1,071 回中 1 回だけ 244 秒に達した。
    // 閾値がここを下回ると、正常な揺らぎで「古い」と言ってしまう
    expect(staleAfterSeconds(SYSTEMS["docomo-cycle"])).toBeGreaterThan(244);
    // HELLO の実測の最大欠損は 305 秒（2026-09-07 の日次品質）
    expect(staleAfterSeconds(SYSTEMS["hellocycling"])).toBeGreaterThan(305);
  });

  it("倍率は 3", () => {
    expect(STALE_CADENCE_MULTIPLIER).toBe(3);
  });
});

describe("isStale", () => {
  const cadence = SYSTEMS["docomo-cycle"];

  it("閾値のちょうど手前は古くない", () => {
    expect(isStale({ cadence, last_observed_at: ago(303), now: NOW })).toBe(false);
  });

  it("閾値を超えたら古い", () => {
    expect(isStale({ cadence, last_observed_at: ago(304), now: NOW })).toBe(true);
  });

  it("未観測は古い扱い（「まだ取れていない」を「新しい」と言わない）", () => {
    expect(isStale({ cadence, last_observed_at: null, now: NOW })).toBe(true);
  });

  it("未来の時刻は古くない（時計のずれで false になるだけ）", () => {
    expect(isStale({ cadence, last_observed_at: ago(-10), now: NOW })).toBe(false);
  });
});
