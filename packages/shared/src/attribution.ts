/**
 * ODPT / 事業者データのクレジット表示と免責。
 * 表示義務の根拠は開発プラン §3.7（CC BY 4.0 の「改変して利用する場合」の書式と
 * ODPT 開発者ガイドライン 3.1 の通知文）。アプリ・Web・API で同じ文言を使う。
 */
import { SYSTEMS } from "./constants";
import type { Attribution } from "./api";

export const CC_BY_4_0_URL = "https://creativecommons.org/licenses/by/4.0/deed.ja";

export const ATTRIBUTIONS: readonly Attribution[] = Object.values(SYSTEMS).map((system) => ({
  system_id: system.system_id,
  provider: system.operator_name,
  dataset: system.dataset_name,
  license: "クリエイティブ・コモンズ・ライセンス 表示4.0国際",
  license_url: system.license_url,
}));

/** CC BY 4.0「改変して利用する場合」の 1 行を組み立てる。 */
export const formatCredit = (attribution: Attribution): string =>
  `${attribution.provider}、${attribution.dataset}、${attribution.license}（${attribution.license_url}）`;

/** アプリ内クレジット画面に表示する全文。 */
export const CREDIT_TEXT = [
  "このアプリは、以下の著作物を改変して利用しています。",
  ...ATTRIBUTIONS.map(formatCredit),
].join("\n");

/** ODPT 開発者ガイドライン 3.1 の通知文。問い合わせ先は環境変数で差し替える。 */
export const buildOdptNotice = (contactEmail: string): string =>
  [
    "本アプリケーションが利用する公共交通データは、公共交通オープンデータセンターにおいて提供されるものです。",
    "公共交通事業者により提供されたデータを元にしていますが、必ずしも正確・完全なものとは限りません。本アプリケーションの表示内容について、公共交通事業者への直接の問合せは行わないでください。",
    "本アプリケーションに関するお問い合わせは、以下のメールアドレスにお願いします。",
    contactEmail,
  ].join("\n");

/** 予測値に関する独自の免責。 */
export const FORECAST_DISCLAIMER =
  "表示される台数・確率は公共交通オープンデータセンターで提供されるデータを基に当アプリが独自に予測したものであり、各事業者が提供・保証するものではありません。";

/**
 * レスポンスヘッダ X-Data-Attribution に載せる 1 行表現。
 * HTTP ヘッダ値は Latin-1（ByteString）しか運べないため ASCII 表記を使い、
 * 日本語の正式なクレジットは /v1/meta の attribution で返す。
 */
export const ATTRIBUTION_HEADER_VALUE = [
  ...Object.values(SYSTEMS).map((system) => `${system.operator_name_ascii} (CC BY 4.0)`),
  "full credit at /v1/meta",
].join("; ");
