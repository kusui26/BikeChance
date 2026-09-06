import { describe, expect, it } from "vitest";
import { isAuthorizedCronRequest } from "./auth";

const SECRET = "s3cret-value-long-enough";

describe("isAuthorizedCronRequest", () => {
  it("正しい Bearer トークンを受け入れる", () => {
    expect(isAuthorizedCronRequest(`Bearer ${SECRET}`, SECRET)).toBe(true);
  });

  it("値が違えば拒否する", () => {
    expect(isAuthorizedCronRequest("Bearer wrong-value-here", SECRET)).toBe(false);
  });

  it("長さが違っても例外にならず拒否する（timingSafeEqual の罠）", () => {
    expect(isAuthorizedCronRequest("Bearer x", SECRET)).toBe(false);
    expect(isAuthorizedCronRequest(`Bearer ${SECRET}${SECRET}`, SECRET)).toBe(false);
  });

  it("Bearer の前置きが無ければ拒否する", () => {
    expect(isAuthorizedCronRequest(SECRET, SECRET)).toBe(false);
  });

  it("大文字小文字の違いを受け入れない", () => {
    expect(isAuthorizedCronRequest(`bearer ${SECRET}`, SECRET)).toBe(false);
  });

  it("ヘッダが無ければ拒否する", () => {
    expect(isAuthorizedCronRequest(null, SECRET)).toBe(false);
  });

  it("秘密が未設定なら常に拒否する（設定漏れで素通りさせない）", () => {
    expect(isAuthorizedCronRequest("Bearer anything", undefined)).toBe(false);
    expect(isAuthorizedCronRequest("Bearer ", "")).toBe(false);
  });
});
