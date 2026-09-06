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
import type { FeedName, SystemId } from "./constants";

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
