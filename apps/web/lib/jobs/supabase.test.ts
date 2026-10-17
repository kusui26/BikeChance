import { describe, expect, it } from "vitest";
import { fetchWithTimeout } from "./supabase";

/** 渡された中断の合図を待ち、合図が来たらその理由で落ちる `fetch`（いつまでも応答しない相手）。 */
const hangingFetch = (seen: (AbortSignal | null | undefined)[]): typeof fetch => {
  return async (_input, init) => {
    seen.push(init?.signal);
    return new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        reject(init.signal?.reason);
      });
    });
  };
};

describe("fetchWithTimeout（W7-03）", () => {
  it("**応答しない相手は、時間切れで止まる**（TimeoutError）", async () => {
    const seen: (AbortSignal | null | undefined)[] = [];
    const timed = fetchWithTimeout(20, hangingFetch(seen));

    await expect(
      timed("https://example.invalid/rest/v1/rpc/weather_grid_cells"),
    ).rejects.toMatchObject({
      name: "TimeoutError",
    });
    expect(seen[0]).toBeInstanceOf(AbortSignal);
  });

  it("呼び出し側の中断の合図も効く（時間切れより先なら、そちらで止まる）", async () => {
    const seen: (AbortSignal | null | undefined)[] = [];
    const timed = fetchWithTimeout(60_000, hangingFetch(seen));
    const caller = new AbortController();

    const pending = timed("https://example.invalid/storage/v1/object", { signal: caller.signal });
    caller.abort(new Error("caller gave up"));

    await expect(pending).rejects.toThrow("caller gave up");
  });

  it("応答が間に合えば、そのまま返す（ほかの指定は変えない）", async () => {
    const methods: (string | undefined)[] = [];
    const quick: typeof fetch = async (_input, init) => {
      methods.push(init?.method);
      return new Response("ok", { status: 200 });
    };
    const response = await fetchWithTimeout(1_000, quick)("https://example.invalid/", {
      method: "POST",
    });

    expect(response.status).toBe(200);
    expect(methods).toEqual(["POST"]);
  });
});
