import { describe, expect, it } from "vitest";
import { readContactEmail } from "./contact";

describe("問い合わせ先（W10-07）", () => {
  it("ビルドの CONTACT_EMAIL をそのまま使う", () => {
    expect(readContactEmail({ CONTACT_EMAIL: "support@example.com" })).toBe("support@example.com");
  });

  it("**無ければ null**（頁は「準備中」と出す。`/v1/meta` の控えの宛先は公開の頁に出さない）", () => {
    expect(readContactEmail({})).toBeNull();
    expect(readContactEmail({ CONTACT_EMAIL: undefined })).toBeNull();
  });

  it("メールアドレスでなければ null（空・設定の書き損じ）", () => {
    expect(readContactEmail({ CONTACT_EMAIL: "" })).toBeNull();
    expect(readContactEmail({ CONTACT_EMAIL: "not-an-email" })).toBeNull();
  });
});
