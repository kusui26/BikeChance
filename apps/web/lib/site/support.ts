/**
 * サポートの頁（`/support`。W10-07、W10 プラン §4.7）。**文は利用者が読んで決める。**
 *
 * 数字は共有の定数から組む（取得の間隔・更新の周期・水平・表示の上限と刻み・予測を出さなくなる
 * までの時間）。**画面の言葉に合わせる**：「現在地へ移動」「行程をチェック」「参考値」
 * 「更新が滞っています」「ふだんの傾向」は iOS の表示の文字列と同じ。
 */
import {
  FORECAST_STALE_AFTER_S,
  GRID_INTERVAL_MIN,
  HORIZONS_MIN,
  PROBABILITY_DISPLAY_MAX,
  PROBABILITY_DISPLAY_STEP,
  SYSTEMS,
} from "@bikechance/shared";
import type { TextSection } from "./text";

const SECONDS_PER_MINUTE = 60;
const MINUTES_PER_HOUR = 60;
const PERCENT = 100;
/** 分で割り切れない間隔は、この秒数に丸めて「約 80 秒」と書く。 */
const ROUGH_SECONDS_STEP = 10;

/**
 * 予測を「参考値」にする、もとにした観測の古さ（秒）。**推論の `MAX_STALENESS_S`
 * （`apps/ml/bikechance_ml/features/constants.py`）と同じ**——確度 1 になり、アプリが「参考値」と出す。
 * 2 つの言語にまたがるので、試験で Python の定義と突き合わせる。
 */
export const REFERENCE_AFTER_S = 600;

const ASCII_END = /[A-Za-z0-9]$/;
const ASCII_START = /^[A-Za-z0-9]/;

/** 英数字と和文のあいだに半角の空白を置いてつなぐ（「HELLO CYCLING が」）。 */
export const joinJa = (left: string, right: string): string =>
  ASCII_END.test(left) || ASCII_START.test(right) ? `${left} ${right}` : `${left}${right}`;

/** 「約 5 分」「約 80 秒」——分で割り切れれば分、そうでなければ 10 秒に丸めた秒。 */
export const roughInterval = (interval_s: number): string => {
  if (interval_s % SECONDS_PER_MINUTE === 0) {
    return `約 ${interval_s / SECONDS_PER_MINUTE} 分`;
  }
  return `約 ${Math.round(interval_s / ROUGH_SECONDS_STEP) * ROUGH_SECONDS_STEP} 秒`;
};

/** 「3 時間」「90 分」——時間で割り切れれば時間。 */
export const horizonText = (minutes: number): string =>
  minutes % MINUTES_PER_HOUR === 0 ? `${minutes / MINUTES_PER_HOUR} 時間` : `${minutes} 分`;

const percentText = (probability: number): string => `${Math.round(probability * PERCENT)}%`;

const systems = Object.values(SYSTEMS);

/** 「HELLO CYCLING とドコモ・バイクシェア」。 */
export const SYSTEM_NAMES_TEXT = systems
  .map((system) => system.display_name)
  .reduce((joined, name) => joinJa(joinJa(joined, "と"), name));

const cadencesText = systems
  .map((system) => joinJa(system.display_name, `が${roughInterval(system.expected_cadence_s)}ごと`))
  .join("、");

const pollText = [...new Set(systems.map((system) => system.poll_interval_s))]
  .map(roughInterval)
  .join("・");

export const SUPPORT_LEAD = [
  `BikeChance は、シェアサイクル（${SYSTEM_NAMES_TEXT}）のポートに着いたとき、自転車を借りられる・返せる確率を表示する iPhone アプリです。いまは TestFlight でベータ版を配っています。`,
] as const;

const READ_PROBABILITY: TextSection = {
  heading: "確率はどう読めばよいですか",
  paragraphs: [
    "降水確率と同じ読み方です。「借りられる 80%」は、同じような状況が 10 回あれば、そのうち 8 回ほどは着いたときに自転車が 1 台以上ある、という意味です。返せる確率は、着いたときに返す場所が 1 つ以上空いている見込みです。",
    `必ず借りられる・返せることを約束するものではありません。表示は ${percentText(PROBABILITY_DISPLAY_STEP)} 刻みで、${percentText(PROBABILITY_DISPLAY_MAX)} までにしています。`,
  ],
};

const WHEN_OBSERVED: TextSection = {
  heading: "台数と確率は、いつの時点のものですか",
  paragraphs: [
    `台数には観測した時刻を、確率には予測を作った時刻を添えています。事業者が公開するデータを${pollText}ごとに取りに行っていますが、${joinJa("データそのものは", cadencesText)}に更新されるため、現地の台数とずれることがあります。`,
  ],
};

const HOW_OFTEN: TextSection = {
  heading: "予測はどのくらいの頻度で、どこまで先を出していますか",
  paragraphs: [
    `${GRID_INTERVAL_MIN} 分ごとに、すべてのポートについて、${horizonText(Math.min(...HORIZONS_MIN))}先から ${horizonText(Math.max(...HORIZONS_MIN))}先まで（${HORIZONS_MIN.length} の時点）の確率を作り直しています。`,
  ],
};

const REFERENCE_AND_STALE: TextSection = {
  heading: "「参考値」「更新が滞っています」と出るのはなぜですか",
  paragraphs: [
    `事業者のデータの更新が ${REFERENCE_AFTER_S / SECONDS_PER_MINUTE} 分を超えて止まっているあいだに作った予測は、確度が下がるため「参考値」と添えます。`,
    `データの更新が止まっているときは、台数に「更新が滞っています」と添えます。${FORECAST_STALE_AFTER_S / SECONDS_PER_MINUTE} 分を超えて止まると、予測は出しません。`,
  ],
};

const USUAL_TREND: TextSection = {
  heading: "「ふだんの傾向」とは何ですか",
  paragraphs: [
    "そのポートの過去 4 週の観測から作った、曜日（平日・土曜・日曜と祝日）と時間ごとの「ふだん」の借りやすさ・返しやすさです。いまの台数を見ていないので、予測ではありません。来週までの予定を立てる目安にしてください。データを集めている時間は灰色で出します。",
  ],
};

const COVERAGE: TextSection = {
  heading: "どの事業者・地域に対応していますか",
  paragraphs: [
    joinJa(
      SYSTEM_NAMES_TEXT,
      "の、公共交通オープンデータセンターで公開されているポートに対応しています。ほかの事業者には対応していません。",
    ),
  ],
};

const RESERVATION: TextSection = {
  heading: "自転車の予約や、借りる・返す手続きはできますか",
  paragraphs: [
    "本アプリでは、予約や貸し出し・返却の手続きはできません。各事業者のアプリやサイトをお使いください。本アプリの表示について、事業者に問い合わせることはお控えください（下のお問い合わせ先へお願いします）。",
  ],
};

const LOCATION: TextSection = {
  heading: "位置情報は何に使いますか",
  paragraphs: [
    "地図の「現在地へ移動」ボタンを押したときにだけ使い、現在地の座標はサーバーに送りません。くわしくはプライバシーポリシーをご覧ください。",
  ],
  links: [{ label: "プライバシーポリシー", href: "/privacy" }],
};

const BETA_FEEDBACK: TextSection = {
  heading: "ベータ版の不具合を知らせるには",
  paragraphs: [
    "TestFlight のフィードバックの機能（スクリーンショットを撮ったときなど）から送ってください。下のお問い合わせ先へのメールでも受け付けます。",
  ],
};

export const SUPPORT_FAQ: readonly TextSection[] = [
  READ_PROBABILITY,
  WHEN_OBSERVED,
  HOW_OFTEN,
  REFERENCE_AND_STALE,
  USUAL_TREND,
  COVERAGE,
  RESERVATION,
  LOCATION,
  BETA_FEEDBACK,
];
