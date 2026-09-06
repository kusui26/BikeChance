/**
 * 受信したバイト列から `last_updated` だけを読む（W1 プラン §6.2 の手順 4）。
 *
 * PR 0 では Zod による検証をしない。検証・正規化・配列化は PR C の
 * `packages/gbfs-core` が担う。ここが知りたいのは「この観測をどのパスに置くか」だけ。
 *
 * 読めなければ null を返し、呼び出し側は取得時刻をパスに使って**保存は続ける**。
 * 壊れたフィードでも生データを失わないことを優先する。
 */

/** GBFS の `last_updated` は POSIX 秒。ミリ秒が来た場合などを弾くための妥当範囲。 */
const MIN_PLAUSIBLE_EPOCH_S = 1_577_836_800; // 2020-01-01T00:00:00Z
const MAX_PLAUSIBLE_EPOCH_S = 4_102_444_800; // 2100-01-01T00:00:00Z

const isPlausibleEpochSeconds = (value: unknown): value is number =>
  typeof value === "number" &&
  Number.isInteger(value) &&
  value >= MIN_PLAUSIBLE_EPOCH_S &&
  value <= MAX_PLAUSIBLE_EPOCH_S;

const parseJson = (body: Uint8Array): unknown => {
  try {
    return JSON.parse(new TextDecoder().decode(body));
  } catch {
    return null;
  }
};

/** 読めた場合は POSIX 秒、読めない・値が不審な場合は null。 */
export const readLastUpdated = (body: Uint8Array): number | null => {
  const parsed = parseJson(body);
  if (typeof parsed !== "object" || parsed === null || !("last_updated" in parsed)) {
    return null;
  }
  const { last_updated } = parsed;
  return isPlausibleEpochSeconds(last_updated) ? last_updated : null;
};
