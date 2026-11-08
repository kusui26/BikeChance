/**
 * 頁の文が引く**アプリの画面の言葉**が、iOS の文字列に在るか（W10-07）。
 *
 * 頁は「地図の『現在地へ移動』ボタン」「『参考値』と添えます」のように画面の言葉をそのまま書く。
 * アプリの言葉だけが変わると、頁は**黙って画面と違うことを案内する**ので、ここで突き合わせる。
 * 「ふだんの傾向」は iOS-5（W9）の文字列で、それが main に入ってから足す。
 */
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { PRIVACY_POLICY_SECTIONS } from "./privacy-policy";
import { SUPPORT_FAQ } from "./support";

const IOS_SOURCE_DIRS = ["../../../ios/BikeChance", "../../../ios/BikeChanceCore/Sources"];

const swiftSources = (): string =>
  IOS_SOURCE_DIRS.flatMap((dir) => {
    const root = fileURLToPath(new URL(dir, import.meta.url));
    return readdirSync(root, { recursive: true, encoding: "utf8" })
      .filter((path) => path.endsWith(".swift"))
      .map((path) => readFileSync(join(root, path), "utf8"));
  }).join("\n");

const pageText = (): string =>
  [...PRIVACY_POLICY_SECTIONS, ...SUPPORT_FAQ]
    .flatMap((section) => [
      ...(section.paragraphs ?? []),
      ...(section.items ?? []).map((item) => `${item.term ?? ""}${item.text}`),
    ])
    .join("\n");

/** 頁が「」で引くアプリの言葉。 */
const APP_LABELS = ["現在地へ移動", "行程をチェック", "参考値", "更新が滞っています"] as const;

describe("頁が引くアプリの言葉は、iOS の文字列に在る", () => {
  it.each(APP_LABELS)("「%s」", (label) => {
    expect(pageText()).toContain(label);
    expect(swiftSources()).toContain(`"${label}"`);
  });
});
