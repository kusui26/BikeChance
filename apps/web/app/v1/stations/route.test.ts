/**
 * GET /v1/stations のルート。
 *
 * DB を持たない環境で走るので、確かめるのは「**入力の誤りは DB に触る前に 400**」と
 * ヘッダ・状態コードの規約。問い合わせの中身は `lib/api/stations.test.ts` と
 * `lib/api/read-port.test.ts`。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { V1_MAX_DURATION_S, problemSchema } from "@bikechance/shared";
import { GET, maxDuration } from "./route";

const url = (query: string): Request =>
  new Request(`https://bike-chance.vercel.app/v1/stations${query}`);

afterEach(() => {
  vi.unstubAllEnvs();
});

const problemOf = async (query: string) => {
  const response = await GET(url(query));
  return { status: response.status, problem: problemSchema.parse(await response.json()) };
};

describe("route segment config", () => {
  it("maxDuration が共有定数と一致する", () => {
    expect(maxDuration).toBe(V1_MAX_DURATION_S);
  });

  it("DB が詰まっても既定の 300 秒を待たない", () => {
    expect(maxDuration).toBeLessThan(60);
  });
});

describe("入力の誤り", () => {
  it("bbox が無ければ 400（設定が無い環境でも 503 にしない）", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const { status, problem } = await problemOf("");
    expect(status).toBe(400);
    expect(problem.code).toBe("bbox_missing");
  });

  it("Problem Details の形と Content-Type を守る", async () => {
    const response = await GET(url(""));
    expect(response.headers.get("Content-Type")).toContain("application/problem+json");
    const problem = problemSchema.parse(await response.json());
    expect(problem.type).toBe("/v1/problems/bbox-missing");
    expect(problem.status).toBe(400);
    expect(problem.title).not.toBe("");
  });

  it("誤りの応答はキャッシュさせない（直したのに直らないように見せない）", async () => {
    const response = await GET(url(""));
    expect(response.headers.get("Cache-Control")).toBe("no-store");
  });

  it("大きすぎる bbox は 400", async () => {
    const { status, problem } = await problemOf("?bbox=139.0,35.0,140.0,36.0");
    expect(status).toBe(400);
    expect(problem.code).toBe("bbox_too_large");
  });

  it("未知の system は 400", async () => {
    const { status, problem } = await problemOf("?bbox=139.76,35.67,139.78,35.69&system=nope");
    expect(status).toBe(400);
    expect(problem.code).toBe("unknown_system");
  });

  it("到着の指定の誤りも DB に触る前に 400", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const bbox = "?bbox=139.76,35.67,139.78,35.69";
    const conflict = await problemOf(`${bbox}&at=2026-09-09T12:30:00Z&in_min=30`);
    expect(conflict.status).toBe(400);
    expect(conflict.problem.code).toBe("arrival_conflict");
    const range = await problemOf(`${bbox}&in_min=999`);
    expect(range.status).toBe(400);
    expect(range.problem.code).toBe("arrival_out_of_range");
    const malformed = await problemOf(`${bbox}&at=2026-09-09T12:30:00`);
    expect(malformed.status).toBe(400);
    expect(malformed.problem.code).toBe("arrival_malformed");
  });

  it("到着の誤りの type も一意（クライアントが分岐できる）", async () => {
    const { problem } = await problemOf("?bbox=139.76,35.67,139.78,35.69&in_min=999");
    expect(problem.type).toBe("/v1/problems/arrival-out-of-range");
    expect(problem.title).not.toBe("");
  });

  it("誤りの応答にもクレジットのヘッダを付ける", async () => {
    const response = await GET(url(""));
    expect(response.headers.get("X-Data-Attribution")).toContain("CC BY 4.0");
  });
});

describe("設定が足りないとき", () => {
  it("bbox が正しければ 503（400 と区別できる）", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const { status, problem } = await problemOf("?bbox=139.76,35.67,139.78,35.69");
    expect(status).toBe(503);
    expect(problem.code).toBe("upstream_unavailable");
  });

  it("理由を応答に載せない", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SECRET_KEY", "");
    const body = await (await GET(url("?bbox=139.76,35.67,139.78,35.69"))).text();
    expect(body).not.toContain("SUPABASE");
  });
});
