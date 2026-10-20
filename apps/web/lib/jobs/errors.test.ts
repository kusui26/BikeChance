import { describe, expect, it } from "vitest";
import { createClient } from "@supabase/supabase-js";
import { toJobFailure } from "./errors";

/** 本番で起きた DB の時間切れ（PostgREST の本文の形。2026-10-02・10-15 の `begin_fetch`）。 */
const statementTimeout = {
  code: "57014",
  details: null,
  hint: null,
  message: "canceling statement due to statement timeout",
};

/** 通信が届かなかったときに supabase-js が組む `error`。`details` に接続先と stack が入る。 */
const unreachable = {
  message: "TypeError: fetch failed",
  details:
    "TypeError: fetch failed\n\nCaused by: Error: getaddrinfo ENOTFOUND project.supabase.co (ENOTFOUND)\n    at GetAddrInfoReqWrap.onlookup",
  hint: "",
  code: "",
};

/** 本物の supabase-js に、決まった応答を返す `fetch` を渡して、`{ data, error }` の `error` を取る。 */
const rpcErrorFrom = async (respond: () => Promise<Response>): Promise<unknown> => {
  const client = createClient("https://example.invalid", "test-key", {
    auth: { persistSession: false, autoRefreshToken: false },
    global: { fetch: async () => respond() },
  });
  const { error } = await client.rpc("begin_fetch", { p_system_id: "docomo-cycle" });
  return error;
};

describe("toJobFailure：Error でない失敗（W7-04）", () => {
  it("**PostgREST の失敗は `code: message` で残す**（`[object Object]` にしない）", () => {
    const failure = toJobFailure({ phase: "ingest", cause: statementTimeout });
    expect(failure.message).toBe("57014: canceling statement due to statement timeout");
    expect(failure.phase).toBe("ingest");
    expect(failure.error_name).toBe("Non-Error(object)");
  });

  it("**`details` と `hint` は読まない**（接続先と stack が入る）", () => {
    const failure = toJobFailure({ phase: "ingest", cause: unreachable });
    expect(failure.message).toBe("TypeError: fetch failed");
    expect(failure.message).not.toContain("supabase.co");
  });

  it("`code` が空なら `message` だけ", () => {
    expect(toJobFailure({ phase: "ingest", cause: { message: "x", code: "" } }).message).toBe("x");
  });

  it("伏字はそのまま効く（トークンも、渡した秘密も）", () => {
    // トークンの形を文字どおり書くと、CI の機密情報の検査に掛かる（redact.test.ts と同じく埋め込む）
    const token = "abcdefgh1234";
    const cause = { code: "XX000", message: `bad acl:consumerKey=${token} and s3cr3t-value` };
    const failure = toJobFailure({ phase: "ingest", cause, secrets: ["s3cr3t-value"] });
    expect(failure.message).toBe("XX000: bad acl:consumerKey=*** and ***");
  });

  it("**長い本文は切る**（HTML の失敗のページがそのまま入ることがある）", () => {
    const cause = { message: `<html>${"x".repeat(5000)}</html>` };
    expect(toJobFailure({ phase: "ingest", cause }).message.length).toBe(300);
  });

  it.each([
    ["`message` が文字列でない", { message: 42 }],
    ["入れ子（中までは掘らない）", { error: { message: "inner" } }],
  ])("%s ものは、いままでどおり", (_label, cause) => {
    expect(toJobFailure({ phase: "ingest", cause }).message).toBe("[object Object]");
  });

  it("Error と、オブジェクトでない値は、いままでどおり", () => {
    expect(toJobFailure({ phase: "fetch", cause: new TypeError("fetch failed") })).toMatchObject({
      error_name: "TypeError",
      message: "fetch failed",
    });
    expect(toJobFailure({ phase: "ingest", cause: "plain" }).message).toBe("plain");
    expect(toJobFailure({ phase: "ingest", cause: null }).message).toBe("null");
  });
});

describe("toJobFailure：本物の supabase-js の `error`（形の思い込みを確かめる）", () => {
  it("**DB の失敗の本文**から `code: message` を拾う", async () => {
    const error = await rpcErrorFrom(async () => Response.json(statementTimeout, { status: 500 }));
    expect(toJobFailure({ phase: "ingest", cause: error }).message).toBe(
      "57014: canceling statement due to statement timeout",
    );
  });

  it("**通信が届かない**ときも、接続先を残さない", async () => {
    const error = await rpcErrorFrom(async () => {
      throw new TypeError("fetch failed", {
        cause: new Error("getaddrinfo ENOTFOUND project.supabase.co"),
      });
    });
    const failure = toJobFailure({ phase: "ingest", cause: error });
    expect(failure.message).toBe("TypeError: fetch failed");
    expect(failure.message).not.toContain("supabase.co");
  });

  it("**時間切れ**（天気のクライアントの 10 秒。W7-03）も理由が残る", async () => {
    const error = await rpcErrorFrom(async () => {
      throw new DOMException("The operation was aborted due to timeout", "TimeoutError");
    });
    expect(toJobFailure({ phase: "ingest", cause: error }).message).toBe(
      "TimeoutError: The operation was aborted due to timeout",
    );
  });
});
