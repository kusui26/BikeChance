import { afterEach, describe, expect, it, vi } from "vitest";
import { COLLECT_MAX_DURATION_S } from "@bikechance/shared";
import { GET, maxDuration } from "./route";

const CRON_SECRET = "test-cron-secret-value";

const request = (authorization?: string): Request =>
  new Request("https://bike-chance.vercel.app/api/jobs/collect/hellocycling", {
    headers: authorization === undefined ? {} : { authorization },
  });

const context = (system: string): { params: Promise<{ system: string }> } => ({
  params: Promise.resolve({ system }),
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("route segment config", () => {
  it("maxDuration が共有定数と一致する", () => {
    // Next は静的リテラルしか受け付けないため import できない。ここで乖離を検出する。
    expect(maxDuration).toBe(COLLECT_MAX_DURATION_S);
  });
});

describe("GET /api/jobs/collect/[system] の認証", () => {
  it("Authorization が無ければ 401", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request(), context("hellocycling"));
    expect(response.status).toBe(401);
  });

  it("値が違えば 401", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request("Bearer wrong"), context("hellocycling"));
    expect(response.status).toBe(401);
  });

  it("CRON_SECRET が未設定なら、どんなヘッダでも 401", async () => {
    vi.stubEnv("CRON_SECRET", "");
    const response = await GET(request("Bearer anything"), context("hellocycling"));
    expect(response.status).toBe(401);
  });

  it("401 の本文に秘密の手がかりを載せない", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request("Bearer wrong"), context("hellocycling"));
    const body: unknown = await response.json();
    expect(JSON.stringify(body)).not.toContain(CRON_SECRET);
  });

  it("ジョブの応答をキャッシュさせない", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request(), context("hellocycling"));
    expect(response.headers.get("Cache-Control")).toBe("no-store");
  });
});

describe("GET /api/jobs/collect/[system] の入力検証", () => {
  it("未知のシステムは 400（認証を通った後に判定する）", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request(`Bearer ${CRON_SECRET}`), context("unknown-system"));
    expect(response.status).toBe(400);
  });

  it("認証前にシステム名を検証しない（未認証には常に 401 を返す）", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request(), context("unknown-system"));
    expect(response.status).toBe(401);
  });

  it("環境変数が足りなければ 500 を返し、変数名だけを載せる", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    vi.stubEnv("ODPT_ACCESS_TOKEN", "");
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    vi.stubEnv("CONTACT_EMAIL", "");

    const response = await GET(request(`Bearer ${CRON_SECRET}`), context("hellocycling"));
    expect(response.status).toBe(500);

    const body: unknown = await response.json();
    const serialized = JSON.stringify(body);
    expect(serialized).toContain("ODPT_ACCESS_TOKEN");
    expect(serialized).toContain("SUPABASE_URL");
  });
});
