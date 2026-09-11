/**
 * GET /v1/trip-check のルート。
 *
 * DB を持たない環境で走るので、確かめるのは「**入力の誤りは DB に触る前に 400**」と
 * ヘッダ・状態コードの規約。問い合わせの中身は `lib/api/trip-check.test.ts`。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { V1_MAX_DURATION_S, problemSchema } from "@bikechance/shared";
import { GET, maxDuration } from "./route";

const url = (query: string): Request =>
  new Request(`https://bike-chance.vercel.app/v1/trip-check${query}`);

const TRIP = "?system=hellocycling&from=10139&to=10145&depart_in_min=15";

afterEach(() => {
  vi.unstubAllEnvs();
});

const problemOf = async (query: string) => {
  const response = await GET(url(query));
  return { status: response.status, problem: problemSchema.parse(await response.json()) };
};

/** 設定を空にする。**入力の誤りが 503 に化けないこと**を見るため。 */
const withoutConfig = () => {
  vi.stubEnv("SUPABASE_URL", "");
  vi.stubEnv("SUPABASE_SECRET_KEY", "");
};

describe("route segment config", () => {
  it("maxDuration が共有定数と一致する", () => {
    expect(maxDuration).toBe(V1_MAX_DURATION_S);
  });

  it("DB が詰まっても既定の 300 秒を待たない", () => {
    expect(maxDuration).toBeLessThan(60);
  });
});

describe("入力の誤りは DB に触る前に 400", () => {
  it.each([
    ["", "unknown_system"],
    ["?system=nope&from=a&to=b&depart_in_min=15", "unknown_system"],
    ["?system=hellocycling&depart_in_min=15", "station_missing"],
    ["?system=hellocycling&from=a&to=a&depart_in_min=15", "same_station"],
    ["?system=hellocycling&from=a&to=b", "depart_missing"],
    [`${TRIP}&ride_min=8.5`, "ride_malformed"],
    [`${TRIP}&ride_min=-1`, "ride_out_of_range"],
    ["?system=hellocycling&from=a&to=b&depart_in_min=999", "arrival_out_of_range"],
  ])("%s → %s", async (query, code) => {
    withoutConfig();
    const { status, problem } = await problemOf(query);
    expect(status).toBe(400);
    expect(problem.code).toBe(code);
  });

  it("Problem Details の形と Content-Type を守る", async () => {
    const response = await GET(url(""));
    expect(response.headers.get("Content-Type")).toContain("application/problem+json");
    const problem = problemSchema.parse(await response.json());
    expect(problem.type).toBe("/v1/problems/unknown-system");
    expect(problem.status).toBe(400);
    expect(problem.title).not.toBe("");
  });

  it("誤りの応答はキャッシュさせない（直したのに直らないように見せない）", async () => {
    const response = await GET(url(""));
    expect(response.headers.get("Cache-Control")).toBe("no-store");
  });

  it("誤りの応答にもクレジットのヘッダを付ける", async () => {
    const response = await GET(url(""));
    expect(response.headers.get("X-Data-Attribution")).toContain("CC BY 4.0");
  });

  it("**出発の誤りは depart_* の名前で案内する**（存在しない引数名を出さない）", async () => {
    withoutConfig();
    const { problem } = await problemOf(
      "?system=hellocycling&from=a&to=b&depart_at=2026-09-11T05:00:00Z&depart_in_min=15",
    );
    expect(problem.code).toBe("arrival_conflict");
    expect(problem.detail).toBe("depart_at と depart_in_min は同時に指定できません。");
  });

  it("**`at` / `in_min` は見ない**（`/v1/stations` の引数。指定しても効かない）", async () => {
    withoutConfig();
    // `depart_in_min` が在るので入力としては正しく、DB に進んで 503 になる。
    // **`at` / `in_min` を足したせいで 400 になったりはしない**
    const { status } = await problemOf(`${TRIP}&at=2026-09-11T05:00:00Z&in_min=30`);
    expect(status).toBe(503);
  });

  it("`depart_*` を書き忘れて `in_min` だけ渡すと、黙って通らず 400 になる", async () => {
    withoutConfig();
    const { status, problem } = await problemOf("?system=hellocycling&from=a&to=b&in_min=30");
    expect(status).toBe(400);
    expect(problem.code).toBe("depart_missing");
  });
});

describe("設定が足りないとき", () => {
  it("入力が正しければ 503（400 と区別できる）", async () => {
    withoutConfig();
    const { status, problem } = await problemOf(TRIP);
    expect(status).toBe(503);
    expect(problem.code).toBe("upstream_unavailable");
  });

  it("理由を応答に載せない", async () => {
    withoutConfig();
    const body = await (await GET(url(TRIP))).text();
    expect(body).not.toContain("SUPABASE");
  });
});
