import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DAILY_DISPATCH_MAX_DURATION_S,
  GITHUB_API_TIMEOUT_MS,
  WEATHER_DB_TIMEOUT_MS,
} from "@bikechance/shared";
import { GET, maxDuration } from "./route";

const CRON_SECRET = "test-cron-secret-value";
const MS_PER_S = 1000;

const request = (authorization?: string): Request =>
  new Request("https://bike-chance.vercel.app/api/jobs/daily-dispatch", {
    headers: authorization === undefined ? {} : { authorization },
  });

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("route segment config", () => {
  it("上限が共有定数と一致する", () => {
    // Next は静的リテラルしか受け付けないため import できない。ここで乖離を検出する。
    expect(maxDuration).toBe(DAILY_DISPATCH_MAX_DURATION_S);
  });

  it("**最悪でも上限に収まる**（DB の 3 回と GitHub の 2 回が、どれも時間切れまで待っても）", () => {
    const worst_ms = 3 * WEATHER_DB_TIMEOUT_MS + 2 * GITHUB_API_TIMEOUT_MS;
    expect(worst_ms).toBeLessThan(maxDuration * MS_PER_S);
  });
});

describe("GET /api/jobs/daily-dispatch の認証", () => {
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

describe("GET /api/jobs/daily-dispatch の環境変数", () => {
  it("**トークンが無ければ 500**（3xx は返さない。DB にも GitHub にも触らない）", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    vi.stubEnv("SUPABASE_URL", "https://example.supabase.co");
    vi.stubEnv("SUPABASE_SECRET_KEY", "sb_secret_test");
    vi.stubEnv("GITHUB_DISPATCH_TOKEN", "");
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const response = await GET(request(`Bearer ${CRON_SECRET}`));
    expect(response.status).toBe(500);
    expect(await response.text()).toContain("GITHUB_DISPATCH_TOKEN");
    expect(fetch).not.toHaveBeenCalled();
  });
});
