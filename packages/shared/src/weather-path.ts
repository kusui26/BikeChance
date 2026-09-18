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

/**
 * **分割を投げる前に待つ時間**を、分割の大きさの並びから決める（W5 プラン §12 の 173）。
 *
 * Open-Meteo の無料枠は**地点の数**で数えるので、1 分の予算（`budget`）を超えないように
 * 窓をまたぐ。返すのは「その分割を投げる**前**に待つミリ秒」で、要素数は入力と同じ。
 *
 * ```
 * pacingDelaysMs([100, 100, 100, 100, 100, 100, 2], 500, 65_000)
 *   → [0, 0, 0, 0, 0, 65_000, 0]
 * ```
 *
 * **予算を使い切った次の分割の前**に 1 回だけ待つ。5 分割（500 地点）を投げ、待ち、
 * 残り 2 分割（102 地点）を投げる——1 分あたり最大 500 地点に収まる。
 *
 * **窓の起点は数え直さない。** 経過時間を測って差し引けばもう少し短くできるが、
 * **実時間に依存する関数はテストが時計に縛られる**。取得自体が数秒かかるぶん
 * 余分に待つだけで、失うのは数秒である。
 *
 * **1 つの分割が予算より大きい場合は待たない。** 待っても通らない（分割の大きさを
 * 直すべき問題）ので、ここで無限に待たせない。`WEATHER_BATCH_SIZE`（100）は
 * 予算（500）より小さいので、実際には起こらない。
 */
export const pacingDelaysMs = (
  sizes: readonly number[],
  budget: number,
  windowMs: number,
): readonly number[] => {
  if (budget < 1) {
    throw new Error(`1 分の予算は 1 以上である必要があります: ${budget}`);
  }
  let spent = 0;
  return sizes.map((size) => {
    if (size > budget) {
      spent = 0;
      return 0;
    }
    if (spent + size > budget) {
      spent = size;
      return windowMs;
    }
    spent += size;
    return 0;
  });
};

/** 上の待ち時間の合計。**要約に載せて、枠に近づいたら気づけるようにする。** */
export const totalPacingMs = (sizes: readonly number[], budget: number, windowMs: number): number =>
  pacingDelaysMs(sizes, budget, windowMs).reduce((total, one) => total + one, 0);
