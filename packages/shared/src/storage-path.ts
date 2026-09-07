/**
 * Storage のパス規約（W1 プラン §11.5）。
 *
 *   gbfs-raw/{system_id}/{YYYY}/{MM}/{DD}/{feed}_{epoch_s}.json.gz
 *
 * 日付は **UTC**。Parquet のパーティションも UTC に揃える。人が読む QA 用の
 * `daily_quality.quality_date` だけが JST で、用途が違うことを明示している。
 *
 * 同じ観測（同じ `last_updated`）は同じパスに写像されるため、`upsert: false`
 * で保存すれば重複は 409 として畳まれ、オブジェクト数は重複排除した場合と一致する。
 */
import { FEED_NAMES, type FeedName, type SystemId } from "./constants";

/** 秒精度の POSIX 時刻。GBFS の `last_updated` がこの単位。 */
export type EpochSeconds = number;

const pad2 = (value: number): string => String(value).padStart(2, "0");

/** UTC の年・月・日をゼロ詰めした文字列で返す。 */
const utcDateParts = (date: Date): readonly [string, string, string] => [
  String(date.getUTCFullYear()),
  pad2(date.getUTCMonth() + 1),
  pad2(date.getUTCDate()),
];

/**
 * バケット内のオブジェクトパスを組み立てる（バケット名は含まない）。
 * `epoch_s` はフィードの `last_updated`。読めなかった場合は取得時刻を渡す。
 */
export const rawObjectPath = (params: {
  readonly system_id: SystemId;
  readonly feed: FeedName;
  readonly epoch_s: EpochSeconds;
}): string => {
  const { system_id, feed, epoch_s } = params;
  const [year, month, day] = utcDateParts(new Date(epoch_s * 1000));
  return `${system_id}/${year}/${month}/${day}/${feed}_${epoch_s}.json.gz`;
};

/**
 * オブジェクト名から `{feed, epoch_s}` を読む。`rawObjectPath` の逆向き。
 *
 * 生 JSON からの再構築（W2 の PR B）が、Storage に並んだファイル名から
 * 「どのフィードの、いつの観測か」を復元するために使う。**書く側と読む側の規約を
 * 同じファイルに置く**ことで、片方だけ変えて気づかない事故を防ぐ。
 *
 * 読めない名前には null を返す（例外にしない）。Storage には想定外のファイルが
 * 混ざり得るので、呼び出し側は「対象から外す」だけで済ませたい。
 */
export const parseRawObjectName = (
  name: string,
): { readonly feed: FeedName; readonly epoch_s: EpochSeconds } | null => {
  const matched = /^([a-z_]+)_(\d+)\.json\.gz$/.exec(name);
  if (matched === null) {
    return null;
  }
  const [, feed, digits] = matched;
  const epoch_s = Number(digits);
  if (
    !FEED_NAMES.some((known) => known === feed) ||
    !Number.isSafeInteger(epoch_s) ||
    epoch_s <= 0
  ) {
    return null;
  }
  // filter で絞ったので feed は FeedName。型ガードで確定させる（as を使わない）
  const known = FEED_NAMES.find((candidate) => candidate === feed);
  return known === undefined ? null : { feed: known, epoch_s };
};

/**
 * 開始日から終了日までの UTC の日を `YYYY/MM/DD` で返す（両端を含む）。
 * Storage のプレフィックスを日ごとに組み立てるのに使う。
 */
export const utcDayPathsBetween = (from: Date, to: Date): readonly string[] => {
  // **先に日へ切り捨ててから比べる。** 同じ日の中の時刻の前後で弾いてしまうと、
  // 「その日 1 日分」という素直な指定が通らなくなる
  const cursor = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate()));
  const end = new Date(Date.UTC(to.getUTCFullYear(), to.getUTCMonth(), to.getUTCDate()));
  if (end < cursor) {
    throw new Error("終了日が開始日より前です");
  }
  const days: string[] = [];
  while (cursor <= end) {
    const [year, month, day] = utcDateParts(cursor);
    days.push(`${year}/${month}/${day}`);
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return days;
};
