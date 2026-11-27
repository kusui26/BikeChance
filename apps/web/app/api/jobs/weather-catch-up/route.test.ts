import { afterEach, describe, expect, it, vi } from "vitest";
import { ARCHIVE_WEATHER_MAX_DURATION_S } from "@bikechance/shared";
import { maxDuration as archiveMaxDuration } from "../archive-weather/route";
import { GET, maxDuration } from "./route";

const CRON_SECRET = "test-cron-secret-value";

const request = (authorization?: string): Request =>
  new Request("https://bike-chance.vercel.app/api/jobs/weather-catch-up", {
    headers: authorization === undefined ? {} : { authorization },
  });

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("route segment config", () => {
  it("**17 分のルートと同じ上限**（取るときは同じ手順・同じ最悪の所要）", () => {
    // Next は静的リテラルしか受け付けないため import できない。ここで乖離を検出する。
    expect(maxDuration).toBe(ARCHIVE_WEATHER_MAX_DURATION_S);
    expect(maxDuration).toBe(archiveMaxDuration);
  });
});

describe("GET /api/jobs/weather-catch-up の認証", () => {
  it("Authorization が無ければ 401", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    expect((await GET(request())).status).toBe(401);
  });

  it("CRON_SECRET が違えば 401（本文にも秘密を含めない）", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request("Bearer wrong-value"));
    expect(response.status).toBe(401);
    expect(await response.text()).not.toContain(CRON_SECRET);
  });

  it("キャッシュに載せない", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    expect((await GET(request())).headers.get("cache-control")).toBe("no-store");
  });
});

describe("GET /api/jobs/weather-catch-up の環境変数", () => {
  it("足りなければ 500（3xx は返さない。取りもしない）", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    vi.stubEnv("ODPT_ACCESS_TOKEN", "");
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    vi.stubEnv("CONTACT_EMAIL", "");
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    expect((await GET(request(`Bearer ${CRON_SECRET}`))).status).toBe(500);
    expect(fetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
