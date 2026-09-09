/**
 * `/v1/meta` の組み立て（`lib/api/meta.ts`）。
 *
 * 一番大事なのは「**DB が落ちたときに何を返すか**」で、答えは「200 と『分からない』」。
 * 鮮度を伝える経路が鮮度を伝えられずに 500 で落ちると、クライアントは何も判断できない。
 */
import { describe, expect, it } from "vitest";
import { SYSTEM_IDS, metaResponseSchema } from "@bikechance/shared";
import { buildMeta, servingModelVersion } from "./meta";
import type { FeedRow } from "./read-port";

const NOW = new Date("2026-09-08T00:00:00.000Z");

const feed = (overrides: Partial<FeedRow> = {}): FeedRow => ({
  system_id: "hellocycling",
  display_name: "HELLO CYCLING",
  expected_cadence_s: 300,
  poll_interval_s: 60,
  capacity_is_dynamic: false,
  last_observed_at: "2026-09-07T23:58:00.000Z",
  forecast_model_version: null,
  forecast_generated_at: null,
  ...overrides,
});

const build = (feeds: readonly FeedRow[] | null) =>
  buildMeta({ feeds, now: NOW, contact_email: "test@example.com" });

describe("DB から鮮度を取れたとき", () => {
  it("スキーマに適合する", () => {
    expect(() => metaResponseSchema.parse(build([feed()]))).not.toThrow();
  });

  it("data_updated_at に実際の観測時刻を入れる", () => {
    expect(build([feed()]).feeds[0]?.data_updated_at).toBe("2026-09-07T23:58:00.000Z");
  });

  it("新しければ stale は false", () => {
    expect(build([feed()]).stale).toBe(false);
  });

  it("閾値を超えたら stale", () => {
    // HELLO の stale_after_s は 960 秒
    const old = feed({ last_observed_at: "2026-09-07T23:40:00.000Z" });
    expect(build([old]).stale).toBe(true);
    expect(build([old]).feeds[0]?.stale).toBe(true);
  });

  it("1 つでも古ければ全体を stale にする", () => {
    const fresh = feed();
    const stale = feed({
      system_id: "docomo-cycle",
      display_name: "ドコモ・バイクシェア",
      expected_cadence_s: 81,
      capacity_is_dynamic: true,
      last_observed_at: "2026-09-07T23:00:00.000Z",
    });
    expect(build([fresh, stale]).stale).toBe(true);
  });

  it("未観測（null）は古い扱い", () => {
    expect(build([feed({ last_observed_at: null })]).stale).toBe(true);
  });

  it("stale は予測の有無ではなくデータの鮮度を表す", () => {
    // W2 時点でモデルは無いが、データが新しければ stale にはならない
    const meta = build([feed()]);
    expect(meta.model_version).toBeNull();
    expect(meta.stale).toBe(false);
  });
});

describe("DB から取れなかったとき", () => {
  it("共有定数から形を作って 200 相当を返す", () => {
    const meta = build(null);
    expect(() => metaResponseSchema.parse(meta)).not.toThrow();
    expect(meta.feeds).toHaveLength(SYSTEM_IDS.length);
  });

  it("鮮度は「分からない」＝古い扱い", () => {
    const meta = build(null);
    expect(meta.stale).toBe(true);
    expect(meta.feeds.every((one) => one.stale)).toBe(true);
    expect(meta.feeds.every((one) => one.data_updated_at === null)).toBe(true);
  });

  it("閾値と capacity_is_dynamic は共有定数から埋める", () => {
    const docomo = build(null).feeds.find((one) => one.system_id === "docomo-cycle");
    expect(docomo?.stale_after_s).toBe(303);
    expect(docomo?.capacity_is_dynamic).toBe(true);
  });

  it("クレジットと通知文は常に返す（表示義務は DB に依存しない）", () => {
    const meta = build(null);
    expect(meta.attribution).toHaveLength(2);
    expect(meta.notice).toContain("公共交通事業者への直接の問合せは行わないでください");
    expect(meta.disclaimer).toContain("独自に予測");
  });
});

describe("連絡先", () => {
  it("環境変数が無ければ控えの宛先を使う（通知文を空にしない）", () => {
    const meta = buildMeta({ feeds: null, now: NOW, contact_email: undefined });
    expect(meta.notice).toContain("@");
  });

  it("与えられた宛先を使う", () => {
    expect(build([feed()]).notice).toContain("test@example.com");
  });
});

describe("配信している予測の版（W4 の PR A）", () => {
  const served = (overrides: Partial<FeedRow>): FeedRow =>
    feed({
      forecast_model_version: "b1-2026-09-08",
      forecast_generated_at: "2026-09-07T23:58:30.000Z",
      ...overrides,
    });

  it("推論が済んでいれば、その版を返す", () => {
    expect(build([served({})]).model_version).toBe("b1-2026-09-08");
  });

  it("**1 度も推論していなければ null**（W2 からの意味を変えない）", () => {
    expect(build([feed()]).model_version).toBeNull();
    expect(servingModelVersion([feed()])).toBeNull();
  });

  it("系統で版が違うときは、新しく推論したほうを返す", () => {
    const older = served({ forecast_model_version: "b1-2026-09-07" });
    const newer = served({
      system_id: "docomo-cycle",
      forecast_model_version: "b1-2026-09-08",
      forecast_generated_at: "2026-09-07T23:59:30.000Z",
    });
    expect(servingModelVersion([older, newer])).toBe("b1-2026-09-08");
    // 並び順に依存しない
    expect(servingModelVersion([newer, older])).toBe("b1-2026-09-08");
  });

  it("片方だけ推論済みなら、そちらを返す", () => {
    expect(servingModelVersion([feed(), served({ system_id: "docomo-cycle" })])).toBe(
      "b1-2026-09-08",
    );
  });

  it("DB から取れなければ null（分からないものを版として出さない）", () => {
    expect(build(null).model_version).toBeNull();
  });

  it("版が入っても stale はデータの鮮度のまま", () => {
    expect(build([served({})]).stale).toBe(false);
  });
});
