import { describe, expect, it } from "vitest";
import {
  ATTRIBUTIONS,
  ATTRIBUTION_HEADER_VALUE,
  CREDIT_TEXT,
  CC_BY_4_0_URL,
  buildOdptNotice,
  formatCredit,
} from "./attribution";
import { SYSTEM_IDS } from "./constants";

describe("attribution", () => {
  it("収集対象の全システム分のクレジットを持つ", () => {
    expect(ATTRIBUTIONS.map((a) => a.system_id)).toEqual([...SYSTEM_IDS]);
  });

  it("各クレジットが CC BY 4.0 の日本語 URL を指す", () => {
    for (const attribution of ATTRIBUTIONS) {
      expect(attribution.license_url).toBe(CC_BY_4_0_URL);
    }
  });

  it("クレジット行に提供者名・データセット名・ライセンス URL を含む", () => {
    const first = ATTRIBUTIONS[0];
    expect(first).toBeDefined();
    if (first === undefined) return;
    const line = formatCredit(first);
    expect(line).toContain(first.provider);
    expect(line).toContain(first.dataset);
    expect(line).toContain(CC_BY_4_0_URL);
  });

  it("クレジット全文が改変利用の書式で始まる", () => {
    expect(CREDIT_TEXT.startsWith("このアプリは、以下の著作物を改変して利用しています。")).toBe(
      true,
    );
    expect(CREDIT_TEXT.split("\n")).toHaveLength(ATTRIBUTIONS.length + 1);
  });

  it("ODPT 通知文に問い合わせ先と事業者への問合せ禁止を含む", () => {
    const notice = buildOdptNotice("support@example.com");
    expect(notice).toContain("公共交通オープンデータセンター");
    expect(notice).toContain("直接の問合せは行わないでください");
    expect(notice).toContain("support@example.com");
  });

  it("ヘッダ用の値にトークンなど機密を含まない", () => {
    expect(ATTRIBUTION_HEADER_VALUE).toContain("CC BY 4.0");
    expect(ATTRIBUTION_HEADER_VALUE).not.toMatch(/consumerKey|token/i);
  });

  it("ヘッダ用の値が ASCII のみ", () => {
    // HTTP ヘッダ値は ByteString（Latin-1）。日本語を入れると Response 構築時に例外になる。
    // 実際にヘッダへ載る検証は apps/web の /v1/meta のテストで行う。
    for (const char of ATTRIBUTION_HEADER_VALUE) {
      expect(char.charCodeAt(0)).toBeLessThan(0x80);
    }
  });
});
