import { describe, expect, it } from "vitest";
import { readCollectorEnv, readDispatchEnv } from "./env";

const SECRET_VALUE = "test-secret-key-never-printed";
const TOKEN_VALUE = "test-dispatch-token-never-printed";

const messageOf = (work: () => unknown): string => {
  try {
    work();
  } catch (cause) {
    return cause instanceof Error ? cause.message : String(cause);
  }
  throw new Error("投げなかった");
};

describe("日次の枠を起こすジョブの環境変数（D-50）", () => {
  it("DB の 2 つと GitHub のトークンだけを読む（ODPT のトークンと連絡先は要らない）", () => {
    const env = readDispatchEnv({
      SUPABASE_URL: "https://example.supabase.co",
      SUPABASE_SECRET_KEY: SECRET_VALUE,
      GITHUB_DISPATCH_TOKEN: TOKEN_VALUE,
    });
    expect(env.GITHUB_DISPATCH_TOKEN).toBe(TOKEN_VALUE);
  });

  it("**足りなければ変数名だけを載せて投げる**（値は載せない）", () => {
    const message = messageOf(() =>
      readDispatchEnv({
        SUPABASE_URL: "https://example.supabase.co",
        SUPABASE_SECRET_KEY: SECRET_VALUE,
      }),
    );
    expect(message).toContain("GITHUB_DISPATCH_TOKEN");
    expect(message).not.toContain(SECRET_VALUE);
  });
});

describe("収集ジョブの環境変数（同じ作りを使う）", () => {
  it("足りなければ変数名だけを載せて投げる", () => {
    const message = messageOf(() =>
      readCollectorEnv({
        SUPABASE_URL: "https://example.supabase.co",
        SUPABASE_SECRET_KEY: SECRET_VALUE,
      }),
    );
    expect(message).toContain("ODPT_ACCESS_TOKEN");
    expect(message).toContain("CONTACT_EMAIL");
    expect(message).not.toContain(SECRET_VALUE);
  });
});
