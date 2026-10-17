import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ARCHIVE_WEATHER_MAX_DURATION_S,
  WEATHER_BATCH_WORST_MS,
  WEATHER_RECORD_MARGIN_MS,
} from "@bikechance/shared";
import { GET, maxDuration } from "./route";

const CRON_SECRET = "test-cron-secret-value";
const MS_PER_S = 1000;

const request = (authorization?: string): Request =>
  new Request("https://bike-chance.vercel.app/api/jobs/archive-weather", {
    headers: authorization === undefined ? {} : { authorization },
  });

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("route segment config", () => {
  it("maxDuration が共有定数と一致する", () => {
    // Next は静的リテラルしか受け付けないため import できない。ここで乖離を検出する。
    expect(maxDuration).toBe(ARCHIVE_WEATHER_MAX_DURATION_S);
  });

  it("この上限なら、時間の予算は少なくとも最初の分割を投げられる（W7-01）", () => {
    // 1 分割の最悪 ＋ 記録の余裕より短くすると、1 つも投げずに終わる
    expect(maxDuration * MS_PER_S).toBeGreaterThanOrEqual(
      WEATHER_BATCH_WORST_MS + WEATHER_RECORD_MARGIN_MS,
    );
  });
});

describe("GET /api/jobs/archive-weather の認証", () => {
  it("Authorization が無ければ 401", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    expect((await GET(request())).status).toBe(401);
  });

  it("CRON_SECRET が違えば 401", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    expect((await GET(request("Bearer wrong-value"))).status).toBe(401);
  });

  it("401 は本文にも秘密を含めない", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    const response = await GET(request("Bearer wrong-value"));
    expect(await response.text()).not.toContain(CRON_SECRET);
  });

  it("キャッシュに載せない", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    expect((await GET(request())).headers.get("cache-control")).toBe("no-store");
  });
});

describe("GET /api/jobs/archive-weather の環境変数", () => {
  it("足りなければ 500（3xx は返さない）", async () => {
    vi.stubEnv("CRON_SECRET", CRON_SECRET);
    vi.stubEnv("ODPT_ACCESS_TOKEN", "");
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    vi.stubEnv("CONTACT_EMAIL", "");
    expect((await GET(request(`Bearer ${CRON_SECRET}`))).status).toBe(500);
  });
});
