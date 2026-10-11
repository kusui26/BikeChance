import { describe, expect, it } from "vitest";
import {
  ATTRIBUTIONS,
  ATTRIBUTION_HEADER_VALUE,
  CREDIT_PREFACE,
  CREDIT_TEXT,
  CC_BY_4_0_URL,
  DATA_SOURCES,
  OPEN_METEO_URL,
  buildOdptNotice,
  formatCredit,
} from "./attribution";
import { dataSourceSchema } from "./api";
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

  it("前置きは開発プラン §3.7 の書式そのまま（クライアントは並べるだけ。W6-31）", () => {
    expect(CREDIT_PREFACE).toBe("このアプリは、以下の著作物を改変して利用しています。");
    expect(CREDIT_TEXT.split("\n")[0]).toBe(CREDIT_PREFACE);
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

describe("ODPT 以外の出典（W6-31、開発プラン §6.4）", () => {
  const byId = (id: string) => DATA_SOURCES.find((source) => source.id === id);

  it("天気・祝日・行政区域の 3 つを、応答のスキーマに合う形で持つ", () => {
    expect(DATA_SOURCES.map((source) => source.id)).toEqual([
      "open-meteo",
      "cao-holidays",
      "soumu-local-government-codes",
    ]);
    for (const source of DATA_SOURCES) {
      expect(dataSourceSchema.parse(source)).toEqual(source);
      expect(source.url.startsWith("https://")).toBe(true);
    }
  });

  it("**Open-Meteo は求める文言でリンクを張る**（CC BY 4.0。Weather data by Open-Meteo.com）", () => {
    const openMeteo = byId("open-meteo");
    expect(openMeteo?.link_label).toBe("Weather data by Open-Meteo.com");
    expect(openMeteo?.url).toBe(OPEN_METEO_URL);
    expect(openMeteo?.text).toContain(
      `クリエイティブ・コモンズ・ライセンス 表示4.0国際（${CC_BY_4_0_URL}）`,
    );
    expect(openMeteo?.text).toContain("加工して利用");
  });

  it("**内閣府・総務省は公共データ利用規約（第1.0版）の「加工した場合」の書き方**", () => {
    for (const [id, title, ministry] of [
      ["cao-holidays", "国民の祝日について", "内閣府"],
      ["soumu-local-government-codes", "全国地方公共団体コード", "総務省"],
    ] as const) {
      const source = byId(id);
      expect(source?.text).toBe(`「${title}」（${ministry}）（${source?.url}）を加工して作成`);
    }
  });
});
