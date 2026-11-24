import { describe, expect, it } from "vitest";
import { createClient } from "@supabase/supabase-js";
import { failureName, toJobFailure } from "./errors";

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

/** undici の fetch の失敗の形：`TypeError: fetch failed` の `cause` に、本当の理由と接続先が入る。 */
const fetchFailed = (code: unknown): TypeError =>
  new TypeError("fetch failed", {
    cause: Object.assign(
      new Error("connect ECONNREFUSED https://api.example.invalid/v1/forecast?token=t0ps3cret"),
      { code, hostname: "api.example.invalid", address: "203.0.113.7" },
    ),
  });

describe("toJobFailure：原因のコード（W12 の PR T、W11 プランの所見 247）", () => {
  it("**fetch の失敗は `cause` の `code` を残す**（`fetch failed` だけでは理由が分からない）", () => {
    const failure = toJobFailure({ phase: "fetch", cause: fetchFailed("ECONNREFUSED") });
    expect(failure.cause_code).toBe("ECONNREFUSED");
    expect(failure.error_name).toBe("TypeError");
    expect(failure.message).toBe("fetch failed");
    expect(failureName(failure)).toBe("TypeError(ECONNREFUSED)");
  });

  it("undici の名前（`UND_ERR_*`）も、例外そのものの `code`（Node のシステムエラー）も拾う", () => {
    expect(
      toJobFailure({ phase: "fetch", cause: fetchFailed("UND_ERR_CONNECT_TIMEOUT") }).cause_code,
    ).toBe("UND_ERR_CONNECT_TIMEOUT");
    const own = Object.assign(new Error("socket hang up"), { code: "ECONNRESET" });
    expect(toJobFailure({ phase: "fetch", cause: own }).cause_code).toBe("ECONNRESET");
  });

  it("**URL・接続先・トークンは、どこにも漏れない**（`cause` から読むのは `code` だけ）", () => {
    const failure = toJobFailure({ phase: "fetch", cause: fetchFailed("ECONNREFUSED") });
    const written = JSON.stringify(failure);
    expect(written).not.toContain("example.invalid");
    expect(written).not.toContain("203.0.113.7");
    expect(written).not.toContain("t0ps3cret");
    expect(written).not.toContain("https://");
  });

  it.each([
    ["小文字", "econnreset"],
    ["URL", "https://evil.example/x"],
    ["空白入り", "ECONN RESET"],
    ["1 文字", "E"],
    ["41 文字", `E${"X".repeat(40)}`],
    ["数字で始まる", "57014"],
    ["空", ""],
    ["数", 23],
    ["オブジェクト", { toString: () => "ECONNRESET" }],
  ])("**形が合わない値は落とす**：%s", (_, code) => {
    const failure = toJobFailure({ phase: "fetch", cause: fetchFailed(code) });
    expect(failure.cause_code).toBeUndefined();
    expect("cause_code" in failure).toBe(false);
    expect(failureName(failure)).toBe("TypeError");
  });

  it("**時間切れ（`TimeoutError`）の数のコードは拾わない**", () => {
    const timeout = new DOMException("The operation was aborted due to timeout", "TimeoutError");
    const failure = toJobFailure({ phase: "fetch", cause: timeout });
    expect(failure.error_name).toBe("TimeoutError");
    expect(failure.cause_code).toBeUndefined();
  });

  it("**ふつうのオブジェクト（PostgREST の本文）の `code` は拾わない**（`message` に入れてある）", () => {
    const failure = toJobFailure({
      phase: "ingest",
      cause: { code: "PGRST301", message: "JWT expired" },
    });
    expect(failure.cause_code).toBeUndefined();
    expect(failure.message).toBe("PGRST301: JWT expired");
  });
});
