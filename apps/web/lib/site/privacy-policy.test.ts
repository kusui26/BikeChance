import { describe, expect, it } from "vitest";
import { BBOX_QUANTUM_DEG } from "@bikechance/shared";
import {
  DATABASE_LOG_RETENTION_DAYS,
  HOSTING_LOG_RETENTION_DAYS,
  PRIVACY_CONTACT_HEADING,
  PRIVACY_POLICY_ESTABLISHED,
  PRIVACY_POLICY_LEAD,
  PRIVACY_POLICY_SECTIONS,
} from "./privacy-policy";

const allText = (): string =>
  [
    ...PRIVACY_POLICY_LEAD,
    ...PRIVACY_POLICY_SECTIONS.flatMap((section) => [
      section.heading,
      ...(section.paragraphs ?? []),
      ...(section.items ?? []).map((item) => `${item.term ?? ""}：${item.text}`),
      ...(section.notes ?? []),
    ]),
  ].join("\n");

describe("プライバシーポリシーは、送っているもの・残っている場所と一致する（W10-07、所見 218 の規則）", () => {
  it("地図の範囲は共有の丸めの刻み（0.01 度）で書く", () => {
    expect(BBOX_QUANTUM_DEG).toBe(0.01);
    expect(allText()).toContain(`緯度・経度 ${BBOX_QUANTUM_DEG} 度（約 1 km）の格子に丸めたもの`);
  });

  it("**範囲だけでなく、ポートの識別子と行程の内容も送ると書く**（`V1Client` が送るもの）", () => {
    const text = allText();
    expect(text).toContain("詳細を開いたポートと、お気に入りに登録したポートの識別子");
    expect(text).toContain("出発と到着のポート、出発の時刻、端末で計算した自転車での所要時間");
    expect(text).toContain("到着の時刻を選んだときは、その時刻も送ります");
  });

  it("**残る場所と期間を書く**（ホスティング 30 日・データベース 7 日）", () => {
    expect(HOSTING_LOG_RETENTION_DAYS).toBe(30);
    expect(DATABASE_LOG_RETENTION_DAYS).toBe(7);
    const text = allText();
    expect(text).toContain(`最長 ${HOSTING_LOG_RETENTION_DAYS} 日残ります`);
    expect(text).toContain(
      `最長 ${DATABASE_LOG_RETENTION_DAYS} 日残ります（利用者の IP アドレスは含みません）`,
    );
  });

  it("現在地の座標は送らないと書く", () => {
    expect(allText()).toContain(
      "現在地の座標そのもの、アカウント、氏名・メールアドレス、端末の識別子（広告識別子を含む）は送りません",
    );
  });

  it("節の番号は 1 から順で、お問い合わせ先がその続き", () => {
    PRIVACY_POLICY_SECTIONS.forEach((section, index) => {
      expect(section.heading.startsWith(`${index + 1}. `)).toBe(true);
    });
    expect(PRIVACY_CONTACT_HEADING.startsWith(`${PRIVACY_POLICY_SECTIONS.length + 1}. `)).toBe(
      true,
    );
  });

  it("外へのリンクは https で始まり、制定の日を書く", () => {
    for (const link of PRIVACY_POLICY_SECTIONS.flatMap((section) => section.links ?? [])) {
      expect(link.href.startsWith("https://")).toBe(true);
    }
    expect(allText()).toContain(`制定：${PRIVACY_POLICY_ESTABLISHED}`);
  });
});
