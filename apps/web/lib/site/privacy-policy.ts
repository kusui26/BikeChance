/**
 * プライバシーポリシー（`/privacy`。W10-07、W10 プラン §4.7）。**文は利用者が読んで決める。**
 *
 * **書いてあることは、送っているもの・残っている場所と一致させる**（W6 プランの所見 218 の規則：
 * 「保持しない」は、自分の DB だけでなく通り道（ログ・CDN）でも確かめてから言う）。
 * 2026-11-08 に確かめたこと：
 *
 * * iOS が `/v1` に送るのは、地図の範囲（`bbox`。0.01 度に丸める）と到着の時刻（`at`）、
 *   詳細とお気に入りのポートの識別子、行程の出発・到着のポートと出発の時刻・所要（`V1Client`）
 * * Vercel の要求の記録は検索の条件（クエリ）と User-Agent を持ち、Pro の Observability Plus で
 *   **30 日**残る（公式文書の Runtime Logs）。このチームは Observability Plus が有効（所見 214・218）
 * * `/v1` は Supabase に PostgREST で問い合わせ、範囲の条件がクエリに載る。Supabase の API の記録は
 *   `request.search` を持ち、Pro で **7 日**残る（料金表の「Log retention (API & Database)」）。
 *   問い合わせ元は `/v1` の関数なので、**利用者の IP アドレスは載らない**
 * * 現在地の座標・お気に入りの一覧は端末の中だけ（`PrivacyInfo.xcprivacy`）。所要の計算
 *   （`MKDirections`）と目的地の検索（`MKLocalSearchCompleter`）は Apple の地図のサービスに送る
 */
import { BBOX_QUANTUM_DEG } from "@bikechance/shared";
import type { TextSection } from "./text";

/** ホスティング（Vercel）の要求の記録の保持日数（Pro ＋ Observability Plus）。 */
export const HOSTING_LOG_RETENTION_DAYS = 30;

/** データベース（Supabase）の API の記録の保持日数（Pro）。 */
export const DATABASE_LOG_RETENTION_DAYS = 7;

/** 制定の日。**公開する日（W10 の 11/12 の枠）に合わせる。** ずれたらここを直す。 */
export const PRIVACY_POLICY_ESTABLISHED = "2026年11月12日";

/** 0.01 度 ≒ 1.1 km（緯度方向）。文には「約 1 km」と書く。 */
const QUANTUM_KM_TEXT = "約 1 km";

const APPLE_PRIVACY_URL = "https://www.apple.com/jp/legal/privacy/";

export const PRIVACY_POLICY_LEAD = [
  "BikeChance（以下「本アプリ」）は、シェアサイクルのポートに着いたときに自転車を借りられる・返せる確率を表示する iPhone アプリです。",
  "本アプリがどの情報を使い、どこへ送り、どこにどれだけ残るかを説明します。",
] as const;

const ON_DEVICE: TextSection = {
  heading: "1. 端末の中だけで使う情報",
  items: [
    {
      term: "現在地",
      text: "地図の現在地のボタン（矢印の形。読み上げでは「現在地へ移動」）を押したときにだけ、位置情報の利用の許可を求めます（アプリを使っているあいだだけの許可で、常に使う許可は求めません）。現在地の座標は端末の中で地図を動かすことにだけ使い、本アプリのサーバーには送りません。許可は端末の「設定」からいつでも取り消せます。",
    },
    {
      term: "お気に入り",
      text: "登録したポートの一覧は端末の中に保存します（端末のバックアップに含まれることがあります）。",
    },
    {
      term: "表示の控え",
      text: "通信できないときのために、直近に取得した表示の内容を端末の中に保存します。",
    },
  ],
};

const SENT: TextSection = {
  heading: "2. サーバーに送る情報",
  paragraphs: [
    "地図や詳細の表示に要るデータを本アプリのサーバーから取得するとき、次の情報を送ります。",
  ],
  items: [
    {
      term: "地図の範囲",
      text: `地図に表示している範囲を、緯度・経度 ${BBOX_QUANTUM_DEG} 度（${QUANTUM_KM_TEXT}）の格子に丸めたもの。地図を現在地に合わせているときは、おおよその現在地を含みます。到着の時刻を選んだときは、その時刻も送ります。`,
    },
    {
      term: "選んだポート",
      text: "詳細を開いたポートと、お気に入りに登録したポートの識別子（最新の台数と確率を取得するため）。",
    },
    {
      term: "行程をチェックした内容",
      text: "出発と到着のポート、出発の時刻、端末で計算した自転車での所要時間。",
    },
    {
      term: "通信に伴う情報",
      text: "IP アドレス、アプリと OS の種類と版、日時（インターネットの通信で通常送られるもの）。",
    },
  ],
  notes: [
    "現在地の座標そのもの、アカウント、氏名・メールアドレス、端末の識別子（広告識別子を含む）は送りません。本アプリにアカウントの登録はありません。",
  ],
};

const STORED: TextSection = {
  heading: "3. 情報が残る場所と期間",
  items: [
    { term: "本アプリのデータベース", text: "2. で挙げた情報は保存しません。" },
    {
      term: "ホスティング事業者（Vercel Inc.）",
      text: `本アプリのサーバーを動かす事業者の要求の記録に、2. で挙げた情報（地図の範囲・ポートの識別子・行程の内容・IP アドレスなど）が最長 ${HOSTING_LOG_RETENTION_DAYS} 日残ります。`,
    },
    {
      term: "データベース事業者（Supabase Inc.）",
      text: `サーバーがデータベースに問い合わせた記録に、地図の範囲とポートの識別子が最長 ${DATABASE_LOG_RETENTION_DAYS} 日残ります（利用者の IP アドレスは含みません）。`,
    },
    {
      term: "配信のキャッシュ",
      text: "応答を速く返すため、同じ範囲・同じポートへの応答を一時的に共有のキャッシュに置きます。応答に利用者ごとの情報は含みません。",
    },
  ],
  notes: [
    `この Web サイトを見たときも、見た頁や IP アドレスなどがホスティング事業者の記録に最長 ${HOSTING_LOG_RETENTION_DAYS} 日残ります。`,
    "これらの事業者は国外の事業者で、記録が日本の外で保存されることがあります。",
  ],
};

const PURPOSES: TextSection = {
  heading: "4. 使う目的",
  paragraphs: ["送られた情報は、次のことにだけ使います。"],
  items: [
    { text: "ポートの台数・確率・ふだんの傾向・行程の見通しを表示するため" },
    { text: "障害を調べ、大量の要求などの不正な利用を防ぐため" },
  ],
  notes: [
    "本アプリは、これらの情報を利用者個人と結び付けません（アカウントや端末の識別子を持たないので、結び付ける手段もありません）。広告やトラッキング、利用者の行動の分析には使いません。",
  ],
};

const THIRD_PARTIES: TextSection = {
  heading: "5. 第三者への提供",
  paragraphs: [
    "法令に基づく場合を除き、送られた情報を第三者に提供したり、販売したりしません。3. で挙げたホスティング事業者とデータベース事業者は、本アプリのサーバーとデータベースを動かすためにだけ情報を扱います。",
  ],
};

const EXTERNAL: TextSection = {
  heading: "6. 外部のサービス",
  items: [
    {
      term: "地図と検索（Apple）",
      text: "地図の表示、目的地の検索（入力した文字と地図の範囲）、自転車での所要時間の計算（出発と到着のポートの位置）には、Apple の地図のサービスを使います。これらの情報の扱いは Apple のプライバシーポリシーによります。",
    },
    {
      term: "広告・解析",
      text: "広告、解析、クラッシュ報告のための外部のサービス（SDK）は組み込んでいません。",
    },
    {
      term: "ベータ版（TestFlight）",
      text: "TestFlight でベータ版を配っているあいだ、テスターが送るフィードバックやクラッシュの情報は、Apple の TestFlight を通じて開発者に届きます。その扱いは Apple の規約とプライバシーポリシーによります。",
    },
  ],
  links: [{ label: "Apple のプライバシーポリシー", href: APPLE_PRIVACY_URL }],
};

const CONTACTS_RECEIVED: TextSection = {
  heading: "7. お問い合わせとテスターの招待",
  paragraphs: [
    "お問い合わせのメールでいただいたメールアドレスと内容は、返信と対応のためにだけ使います。ベータ版のテスターとして招待するときに伺ったメールアドレスは、招待と連絡のためにだけ使います。",
  ],
};

const CHANGES: TextSection = {
  heading: "8. このポリシーの変更",
  paragraphs: [
    "このポリシーを変えるときは、この頁で知らせます。",
    `制定：${PRIVACY_POLICY_ESTABLISHED}`,
  ],
};

/** 本文の節（お問い合わせ先は頁が `CONTACT_EMAIL` から足す）。 */
export const PRIVACY_POLICY_SECTIONS: readonly TextSection[] = [
  ON_DEVICE,
  SENT,
  STORED,
  PURPOSES,
  THIRD_PARTIES,
  EXTERNAL,
  CONTACTS_RECEIVED,
  CHANGES,
];

/** お問い合わせ先の節の見出し（番号は本文の続き）。 */
export const PRIVACY_CONTACT_HEADING = "9. お問い合わせ先";
