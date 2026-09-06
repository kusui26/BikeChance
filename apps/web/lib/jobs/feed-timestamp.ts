/**
 * 受信したバイト列を JSON にし、`last_updated` を読む（W1 プラン §6.6 の手順 6・7）。
 *
 * **JSON のパースは 1 回だけ**にする。保存パスを決めるための `last_updated` と、
 * `gbfs-core` の全件検証で、同じ値を使い回す。HELLO の 4.2 MB を 2 回パースすると
 * それだけで 22 ms 余分にかかる（W1 プラン §4.1 (a)）。
 *
 * どちらの関数も**例外を投げない**。壊れたフィードでも生データの保存は続けたい
 * （W1-26）。読めなければ null を返し、呼び出し側は取得時刻をパスに使う。
 *
 * 妥当範囲の定数は `@bikechance/gbfs-core` と共有する。ここと gbfs-core で
 * 「いつからいつまでを妥当とするか」がずれると、保存のパスと DB の値が食い違う。
 */
import { MAX_PLAUSIBLE_EPOCH_S, MIN_PLAUSIBLE_EPOCH_S } from "@bikechance/gbfs-core";

const isPlausibleEpochSeconds = (value: unknown): value is number =>
  typeof value === "number" &&
  Number.isInteger(value) &&
  value >= MIN_PLAUSIBLE_EPOCH_S &&
  value <= MAX_PLAUSIBLE_EPOCH_S;

/** 受信バイト列を JSON にする。壊れていれば null（例外は投げない）。 */
export const parseFeedJson = (body: Uint8Array): unknown => {
  try {
    return JSON.parse(new TextDecoder().decode(body));
  } catch {
    return null;
  }
};

/** パース済みの値から `last_updated`（POSIX 秒）を読む。読めなければ null。 */
export const readLastUpdatedFrom = (document: unknown): number | null => {
  if (typeof document !== "object" || document === null || !("last_updated" in document)) {
    return null;
  }
  const { last_updated } = document;
  return isPlausibleEpochSeconds(last_updated) ? last_updated : null;
};

/** バイト列から直接読む。パース結果を使い回さない場面用。 */
export const readLastUpdated = (body: Uint8Array): number | null =>
  readLastUpdatedFrom(parseFeedJson(body));
