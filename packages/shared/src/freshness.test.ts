import { describe, expect, it } from "vitest";
import { FORECAST_STALE_AFTER_S, SYSTEMS } from "./constants";
import { STALE_CADENCE_MULTIPLIER, isForecastFresh, isStale, staleAfterSeconds } from "./freshness";

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

describe("isForecastFresh", () => {
  const fresh = (seconds: number): boolean =>
    isForecastFresh({ base_observed_at: ago(seconds), now: NOW });

  it("閾値のちょうどは出せる（900 秒）", () => {
    expect(FORECAST_STALE_AFTER_S).toBe(900);
    expect(fresh(FORECAST_STALE_AFTER_S)).toBe(true);
  });

  it("閾値を超えたら出さない（11 時間前の確率を「現在の予測」と言わない）", () => {
    expect(fresh(FORECAST_STALE_AFTER_S + 1)).toBe(false);
    expect(fresh(11 * 60 * 60)).toBe(false);
  });

  it("未計算（null）は古い扱い", () => {
    expect(isForecastFresh({ base_observed_at: null, now: NOW })).toBe(false);
  });

  it("未来の時刻は新しい扱い（時計のずれで消えない）", () => {
    expect(fresh(-10)).toBe(true);
  });

  it("推論の 2 周期ぶんは持つ（1 回落としただけで消えない）", () => {
    // 推論は 5 分毎。1 回落ちても次の回で戻るまで出し続けたい
    expect(fresh(2 * 300)).toBe(true);
  });

  it("**フィードごとの stale_after_s は使わない**（ドコモの 303 秒で明滅させない）", () => {
    // ドコモの stale_after_s は 303 秒で推論周期（300 秒）とほぼ同じ。
    // これで切ると正常運転でも出たり消えたりする
    expect(staleAfterSeconds(SYSTEMS["docomo-cycle"])).toBeLessThan(FORECAST_STALE_AFTER_S);
    expect(fresh(staleAfterSeconds(SYSTEMS["docomo-cycle"]) + 1)).toBe(true);
  });
});
