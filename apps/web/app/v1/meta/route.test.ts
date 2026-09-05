import { describe, expect, it } from "vitest";
import { metaResponseSchema } from "@bikechance/shared";
import { GET } from "./route";

describe("GET /v1/meta", () => {
  it("スキーマに適合した JSON を返す", async () => {
    const response = GET();
    expect(response.status).toBe(200);

    const body: unknown = await response.json();
    const parsed = metaResponseSchema.parse(body);
    expect(parsed.api_version).toBe("v1");
    expect(parsed.feeds).toHaveLength(2);
    expect(parsed.stale).toBe(true);
  });

  it("キャッシュとクレジットのヘッダを付ける", () => {
    const response = GET();
    expect(response.headers.get("Cache-Control")).toContain("s-maxage=60");

    // ヘッダ値は Latin-1 しか運べないため、日本語を入れると Response 構築時に例外になる
    const attribution = response.headers.get("X-Data-Attribution");
    expect(attribution).toContain("CC BY 4.0");
    expect(attribution).toMatch(/^[\x20-\x7e]+$/);
  });

  it("日本語の正式なクレジットはボディで返す", async () => {
    const body: unknown = await GET().json();
    const parsed = metaResponseSchema.parse(body);
    expect(parsed.attribution.map((a) => a.provider).join()).toContain(
      "公共交通オープンデータ協議会",
    );
    expect(parsed.notice).toContain("公共交通事業者への直接の問合せは行わないでください");
  });
});
