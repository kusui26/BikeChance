/**
 * GET /v1/stations/{system}/{station_id} のルート。
 *
 * DB を持たない環境で走るので、確かめるのは「**経路の誤りは DB に触る前に 400**」と
 * ヘッダ・状態コードの規約。問い合わせの中身は `lib/api/station-detail.test.ts`。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { V1_CACHE_CONTROL, V1_MAX_DURATION_S, problemSchema } from "@bikechance/shared";
import { GET, maxDuration } from "./route";

/** 経路の引数は Promise で届く（Next.js 16）。 */
const context = (system: string, station_id: string) => ({
  params: Promise.resolve({ system, station_id }),
});

const request = (): Request =>
  new Request("https://bike-chance.vercel.app/v1/stations/hellocycling/s-1");

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("route segment config", () => {
  it("maxDuration が共有定数と一致する", () => {
    expect(maxDuration).toBe(V1_MAX_DURATION_S);
  });

  it("DB が詰まっても既定の 300 秒を待たない", () => {
    expect(maxDuration).toBeLessThan(60);
  });
});

describe("経路の誤り", () => {
  it("知らないシステムは 400（設定が無くても 503 に化けない）", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const response = await GET(request(), context("unknown", "s-1"));
    expect(response.status).toBe(400);
    const problem = problemSchema.parse(await response.json());
    expect(problem.code).toBe("unknown_system");
    expect(problem.type).toBe("/v1/problems/unknown-system");
  });

  it("空の station_id は 404", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const response = await GET(request(), context("hellocycling", ""));
    expect(response.status).toBe(404);
    expect(problemSchema.parse(await response.json()).code).toBe("station_not_found");
  });

  it("**誤りの応答にもクレジットのヘッダを付ける**（表示の義務は状態で変わらない）", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const response = await GET(request(), context("unknown", "s-1"));
    expect(response.headers.get("X-Data-Attribution")).not.toBeNull();
  });

  it("設定が足りなければ 503（理由は載せない）", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const response = await GET(request(), context("hellocycling", "s-1"));
    expect(response.status).toBe(503);
    const problem = problemSchema.parse(await response.json());
    expect(problem.code).toBe("upstream_unavailable");
    expect(problem.detail).not.toMatch(/SUPABASE/);
  });
});

describe("キャッシュ", () => {
  it("**`/v1/stations` と同じ `Cache-Control`**（ここだけ別の値にしない）", () => {
    expect(V1_CACHE_CONTROL).toMatch(/s-maxage=\d+/);
  });
});
