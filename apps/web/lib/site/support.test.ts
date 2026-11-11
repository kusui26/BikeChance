import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { GRID_INTERVAL_MIN, INFER_CRON, SYSTEMS, SYSTEM_IDS } from "@bikechance/shared";
import {
  REFERENCE_AFTER_S,
  SUPPORT_FAQ,
  SUPPORT_LEAD,
  SYSTEM_NAMES_TEXT,
  horizonText,
  joinJa,
  roughInterval,
} from "./support";

const allText = (): string =>
  [
    ...SUPPORT_LEAD,
    ...SUPPORT_FAQ.flatMap((section) => [section.heading, ...(section.paragraphs ?? [])]),
  ].join("\n");

describe("サポートの文の部品（W10-07）", () => {
  it("間隔は分で割り切れれば分、そうでなければ 10 秒に丸めた秒", () => {
    expect(roughInterval(60)).toBe("約 1 分");
    expect(roughInterval(300)).toBe("約 5 分");
    expect(roughInterval(81)).toBe("約 80 秒");
    expect(roughInterval(85)).toBe("約 90 秒");
  });

  it("水平は時間で割り切れれば時間", () => {
    expect(horizonText(5)).toBe("5 分");
    expect(horizonText(90)).toBe("90 分");
    expect(horizonText(180)).toBe("3 時間");
  });

  it("英数字と和文のあいだにだけ空白を置く", () => {
    expect(joinJa("HELLO CYCLING", "が")).toBe("HELLO CYCLING が");
    expect(joinJa("データは", "HELLO")).toBe("データは HELLO");
    expect(joinJa("ドコモ・バイクシェア", "が")).toBe("ドコモ・バイクシェアが");
  });

  it("事業者の名前は共有の定義から（「HELLO CYCLING とドコモ・バイクシェア」）", () => {
    for (const id of SYSTEM_IDS) {
      expect(SYSTEM_NAMES_TEXT).toContain(SYSTEMS[id].display_name);
    }
    expect(SYSTEM_NAMES_TEXT).toBe("HELLO CYCLING とドコモ・バイクシェア");
  });
});

describe("サポートの文は、動いている作りと一致する", () => {
  it("取得の間隔と、事業者ごとの更新の周期を書く", () => {
    const text = allText();
    expect(text).toContain("データを約 1 分ごとに取りに行っています");
    expect(text).toContain("HELLO CYCLING が約 5 分ごと、ドコモ・バイクシェアが約 80 秒ごとに更新");
  });

  it("予測の周期と水平（5 分先から 3 時間先までの 10 の時点）", () => {
    expect(allText()).toContain(
      "5 分ごとに、すべてのポートについて、5 分先から 3 時間先まで（10 の時点）",
    );
  });

  it("**予測の周期は推論の定時と同じ**（`INFER_CRON` の刻み ＝ `GRID_INTERVAL_MIN`）", () => {
    for (const cron of Object.values(INFER_CRON)) {
      const step = /^\d+-59\/(\d+) \* \* \* \*$/.exec(cron)?.[1];
      expect(Number(step)).toBe(GRID_INTERVAL_MIN);
    }
  });

  it("**「参考値」の閾値は推論の `MAX_STALENESS_S` と同じ**（Python の定義を読む）", () => {
    const file = fileURLToPath(
      new URL("../../../ml/bikechance_ml/features/constants.py", import.meta.url),
    );
    const defined = /^MAX_STALENESS_S: Final\[int\] = (\d+)$/m.exec(
      readFileSync(file, "utf8"),
    )?.[1];
    expect(Number(defined)).toBe(REFERENCE_AFTER_S);
    expect(allText()).toContain("10 分を超えて止まっているあいだに作った予測");
  });

  it("表示の刻みと上限（5% 刻み・99% まで）と、予測を出さなくなる 15 分", () => {
    const text = allText();
    expect(text).toContain("表示は 5% 刻みで、99% までにしています");
    expect(text).toContain("15 分を超えて止まると、予測は出しません");
  });

  it("問いは重ならず、頁の中のリンクは / で、外は https で始まる", () => {
    const headings = SUPPORT_FAQ.map((section) => section.heading);
    expect(new Set(headings).size).toBe(headings.length);
    for (const link of SUPPORT_FAQ.flatMap((section) => section.links ?? [])) {
      expect(link.href.startsWith("/") || link.href.startsWith("https://")).toBe(true);
    }
  });
});
