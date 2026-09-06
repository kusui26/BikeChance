import { describe, expect, it } from "vitest";
import { redact, redactConsumerKey, redactLiteral } from "./redact";

const TOKEN = "abcdefghij0123456789ABCDEF";

describe("redactConsumerKey", () => {
  it("クエリのトークンを伏字にし、キー名は残す", () => {
    const text = `https://api.odpt.org/api/v4/gbfs/hellocycling/station_status.json?acl:consumerKey=${TOKEN}`;
    const redacted = redactConsumerKey(text);
    expect(redacted).not.toContain(TOKEN);
    expect(redacted).toContain("acl:consumerKey=***");
  });

  it("後続のパラメータを巻き込まない", () => {
    const redacted = redactConsumerKey(`?acl:consumerKey=${TOKEN}&format=json`);
    expect(redacted).toBe("?acl:consumerKey=***&format=json");
  });

  it("引用符やタグの手前で止まる（JSON やログに埋まっていても壊さない）", () => {
    const redacted = redactConsumerKey(`{"url":"?acl:consumerKey=${TOKEN}"}`);
    expect(redacted).toBe('{"url":"?acl:consumerKey=***"}');
  });

  it("複数回の出現をすべて伏字にする", () => {
    const redacted = redactConsumerKey(
      `a=${TOKEN} acl:consumerKey=${TOKEN} acl:consumerKey=${TOKEN}`,
    );
    expect(redacted.match(/\*\*\*/g)).toHaveLength(2);
  });

  it("大文字小文字を区別しない", () => {
    expect(redactConsumerKey(`?ACL:CONSUMERKEY=${TOKEN}`)).toContain("***");
  });

  it("トークンを含まない文字列は変えない", () => {
    const text = "fetch failed";
    expect(redactConsumerKey(text)).toBe(text);
  });
});

describe("redactLiteral", () => {
  it("前置きが無くても既知のトークン値そのものを伏字にする", () => {
    const redacted = redactLiteral(`token was ${TOKEN} here`, TOKEN);
    expect(redacted).toBe("token was *** here");
  });

  it("短すぎる秘密では置換しない（文章が壊れるため）", () => {
    expect(redactLiteral("a-b-a-b", "a")).toBe("a-b-a-b");
  });

  it("空文字を渡しても文字列を分解しない", () => {
    expect(redactLiteral("hello", "")).toBe("hello");
  });
});

describe("redact", () => {
  it("クエリ形式と裸の値の両方を伏字にする", () => {
    const text = `url=?acl:consumerKey=${TOKEN} raw=${TOKEN}`;
    const redacted = redact(text, [TOKEN]);
    expect(redacted).not.toContain(TOKEN);
  });

  it("秘密を渡さなくてもクエリ形式は伏字になる", () => {
    expect(redact(`?acl:consumerKey=${TOKEN}`)).not.toContain(TOKEN);
  });
});
