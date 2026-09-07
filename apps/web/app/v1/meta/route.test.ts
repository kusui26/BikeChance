/**
 * GET /v1/meta のルート。
 *
 * DB を持たない環境で走るので、ここで確かめるのは「**DB に届かなくても 200 で
 * 正しい形を返す**」ことと、ヘッダの規約。鮮度の計算そのものは `lib/api/meta.test.ts`。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { V1_MAX_DURATION_S, metaResponseSchema } from "@bikechance/shared";
import { GET, maxDuration } from "./route";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("route segment config", () => {
  it("maxDuration が共有定数と一致する", () => {
    // Next は静的リテラルしか受け付けないため import できない。ここで乖離を検出する
    expect(maxDuration).toBe(V1_MAX_DURATION_S);
  });
});

describe("GET /v1/meta", () => {
  it("DB に届かなくても 200 とスキーマ適合の JSON を返す", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const response = await GET();
    expect(response.status).toBe(200);

    const parsed = metaResponseSchema.parse(await response.json());
    expect(parsed.api_version).toBe("v1");
    expect(parsed.feeds).toHaveLength(2);
    // 鮮度が分からないときは「古い」と言う
    expect(parsed.stale).toBe(true);
  });

  it("キャッシュとクレジットのヘッダを付ける", async () => {
    const response = await GET();
    expect(response.headers.get("Cache-Control")).toContain("s-maxage=60");

    // ヘッダ値は Latin-1 しか運べないため、日本語を入れると Response 構築時に例外になる
    const attribution = response.headers.get("X-Data-Attribution");
    expect(attribution).toContain("CC BY 4.0");
    expect(attribution).toMatch(/^[\x20-\x7e]+$/);
  });

  it("日本語の正式なクレジットはボディで返す", async () => {
    const parsed = metaResponseSchema.parse(await (await GET()).json());
    expect(parsed.attribution.map((a) => a.provider).join()).toContain(
      "公共交通オープンデータ協議会",
    );
    expect(parsed.notice).toContain("公共交通事業者への直接の問合せは行わないでください");
  });

  it("鮮度の閾値を応答に載せる（クライアントが同じ判定をできる）", async () => {
    const parsed = metaResponseSchema.parse(await (await GET()).json());
    expect(parsed.feeds.every((feed) => feed.stale_after_s > 0)).toBe(true);
  });
});
