/**
 * ODPT / 事業者データのクレジット表示と免責。
 * 表示義務の根拠は開発プラン §3.7（CC BY 4.0 の「改変して利用する場合」の書式と
 * ODPT 開発者ガイドライン 3.1 の通知文）。アプリ・Web・API で同じ文言を使う。
 */
import { SYSTEMS } from "./constants";
import type { Attribution, DataSource } from "./api";

export const CC_BY_4_0_URL = "https://creativecommons.org/licenses/by/4.0/deed.ja";

/**
 * CC BY 4.0「改変して利用する場合」の前置き（開発プラン §3.7、ODPT の FAQ の書式）。
 * **クレジットの一覧の前に置く。** `attribution` を返す応答は、すべてこれも返す（W6-31）。
 */
export const CREDIT_PREFACE = "このアプリは、以下の著作物を改変して利用しています。";

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
export const CREDIT_TEXT = [CREDIT_PREFACE, ...ATTRIBUTIONS.map(formatCredit)].join("\n");

export const OPEN_METEO_URL = "https://open-meteo.com/";
const CAO_HOLIDAYS_URL = "https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html";
const SOUMU_LOCAL_GOVERNMENT_CODES_URL = "https://www.soumu.go.jp/denshijiti/code.html";

/**
 * ODPT 以外の出典（W6-31、開発プラン §6.4）。**提供元ごとに求める書式が違う**ので、
 * 表示する 1 行はここで組み、クライアントは並べてリンクを張るだけにする。
 *
 * * **天気（Open-Meteo）**：データは CC BY 4.0。Open-Meteo は「Weather data by
 *   Open-Meteo.com」を https://open-meteo.com/ へのリンクとして出すよう求めている。
 *   予報（気象庁 MSM と best_match）を予測の材料に加工して使うので、CC BY 4.0 の
 *   「改変して利用する場合」と同じ並び（提供者、名称、ライセンス）で書く
 * * **祝日（内閣府）・行政区域（総務省）**：両府省のサイトは**公共データ利用規約
 *   （第1.0版。CC BY 4.0 と互換）**で提供している。規約の「加工した場合」の記載例
 *   （「○○」（府省名）（当該ページの URL）を加工して作成）のとおりに書く
 */
export const DATA_SOURCES: readonly DataSource[] = [
  {
    id: "open-meteo",
    text: `Open-Meteo、天気予報（気象庁 MSM ほか。予測の材料に加工して利用）、クリエイティブ・コモンズ・ライセンス 表示4.0国際（${CC_BY_4_0_URL}）`,
    link_label: "Weather data by Open-Meteo.com",
    url: OPEN_METEO_URL,
  },
  {
    id: "cao-holidays",
    text: `「国民の祝日について」（内閣府）（${CAO_HOLIDAYS_URL}）を加工して作成`,
    link_label: "内閣府「国民の祝日について」",
    url: CAO_HOLIDAYS_URL,
  },
  {
    id: "soumu-local-government-codes",
    text: `「全国地方公共団体コード」（総務省）（${SOUMU_LOCAL_GOVERNMENT_CODES_URL}）を加工して作成`,
    link_label: "総務省「全国地方公共団体コード」",
    url: SOUMU_LOCAL_GOVERNMENT_CODES_URL,
  },
];

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
