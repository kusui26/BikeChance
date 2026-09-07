import { afterEach, describe, expect, it, vi } from "vitest";
import { SYNC_STATIONS_MAX_DURATION_S } from "@bikechance/shared";
import { GET, maxDuration } from "./route";

const CRON_SECRET = "test-cron-secret-value";

const request = (authorization?: string): Request =>
  new Request("https://bike-chance.vercel.app/api/jobs/sync-stations/hellocycling", {
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
    expect(maxDuration).toBe(SYNC_STATIONS_MAX_DURATION_S);
  });

  it("収集より長い（7.8 MB の station_information を 1 回で処理するため）", () => {
    expect(maxDuration).toBeGreaterThan(60);
  });
});

describe("GET /api/jobs/sync-stations/[system] の認証", () => {
  it("Authorization が無ければ 401", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request(), context("hellocycling"));
    expect(response.status).toBe(401);
  });

  it("CRON_SECRET が違えば 401", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request("Bearer wrong-value"), context("hellocycling"));
    expect(response.status).toBe(401);
  });

  it("401 は本文にも秘密を含めない", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request("Bearer wrong-value"), context("hellocycling"));
    expect(await response.text()).not.toContain(CRON_SECRET);
  });

  it("キャッシュに載せない", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request(), context("hellocycling"));
    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});

describe("GET /api/jobs/sync-stations/[system] の入力検証", () => {
  it("未知のシステムは 400", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request(`Bearer ${CRON_SECRET}`), context("unknown-system"));
    expect(response.status).toBe(400);
  });

  it("環境変数が足りなければ 500（3xx は返さない）", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    vi.stubEnv("ODPT_ACCESS_TOKEN", "");
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    vi.stubEnv("CONTACT_EMAIL", "");
    const response = await GET(request(`Bearer ${CRON_SECRET}`), context("hellocycling"));
    expect(response.status).toBe(500);
  });
});
