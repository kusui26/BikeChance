/**
 * 天気アーカイブのパス規約（W2 プラン §9.1）。
 *
 *   weather-raw/{YYYY}/{MM}/{DD}/jma_msm_{hour_epoch_s}_{NN}.json.gz
 *
 * 日付は **UTC**（`gbfs-raw` と同じ。W1 プラン §11.5）。
 *
 * **時刻は「時」に丸める。** 同じ時間内に 2 回動いても同じパスに写像されるので、
 * `upsert: false` で保存すれば 2 回目は 409 として畳まれる。取り直しが安全になり、
 * 1 時間に 1 個という数え方も単純になる。分秒の精度は失うが、予報の発行は
 * 3 時間毎で、どの時刻に取ったかは応答自体からは分からない（発行時刻を返さない）
 * ため、失うものは無い。
 *
 * `NN` は分割の連番。どの格子が入っているかは応答の `latitude` / `longitude` に
 * 書いてあるので、パスには持たせない（要求した座標と応答の座標はずれる）。
 */
import { OPEN_METEO_MODEL } from "./constants";
import type { EpochSeconds } from "./storage-path";

const SECONDS_PER_HOUR = 3600;
const MS_PER_S = 1000;

const pad2 = (value: number): string => String(value).padStart(2, "0");

/** POSIX 秒を「時」の境界に切り下げる。 */
export const truncateToHour = (epoch_s: EpochSeconds): EpochSeconds =>
  Math.floor(epoch_s / SECONDS_PER_HOUR) * SECONDS_PER_HOUR;

/**
 * バケット内のオブジェクトパスを組み立てる（バケット名は含まない）。
 * `epoch_s` は取得を始めた時刻。内部で「時」に丸める。
 */
export const weatherObjectPath = (params: {
  readonly epoch_s: EpochSeconds;
  readonly batch: number;
}): string => {
  const hour_epoch_s = truncateToHour(params.epoch_s);
  const at = new Date(hour_epoch_s * MS_PER_S);
  const year = String(at.getUTCFullYear());
  const month = pad2(at.getUTCMonth() + 1);
  const day = pad2(at.getUTCDate());
  return `${year}/${month}/${day}/${OPEN_METEO_MODEL}_${hour_epoch_s}_${pad2(params.batch)}.json.gz`;
};

/**
 * 格子を `WEATHER_BATCH_SIZE` ごとに分ける。順序は入力のまま保つ
 * （応答と要求の対応を人が追えるようにするため）。
 */
export const batchCells = <T>(cells: readonly T[], size: number): readonly (readonly T[])[] => {
  if (size < 1) {
    throw new Error(`分割の大きさは 1 以上である必要があります: ${size}`);
  }
  const batches: T[][] = [];
  for (let start = 0; start < cells.length; start += size) {
    batches.push([...cells.slice(start, start + size)]);
  }
  return batches;
};
