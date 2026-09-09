/**
 * 「その値はまだ現在値と言えるか」の判定（開発プラン §8.3、W2 プラン §5.7）。
 *
 * **監視（migration 0013 の `monitor_feeds`）と同じ式にする。** 片方だけを変えると、
 * 「API は古いと言っているのに監視は鳴らない」あるいはその逆、という食い違いが起きる。
 *
 * 式に取得間隔を足すのは W1-42 の学び。`now() - last_observed_at` が表しているのは
 * 「公開されてからの時間」ではなく「**公開間隔＋こちらが取りに行くまでの時間**」で、
 * 検知の遅れを足さないと正常な揺らぎで古い判定が出る（ドコモの実測で 244 秒 > 240 秒）。
 */

import { FORECAST_STALE_AFTER_S } from "./constants";

/** 期待周期の何倍まで待つか。0013 の `expected_cadence_s * 3` と揃える。 */
export const STALE_CADENCE_MULTIPLIER = 3;

const MS_PER_S = 1000;

export type FeedCadence = {
  /** フィードの実測更新周期（秒）。HELLO 300 / ドコモ 81。 */
  readonly expected_cadence_s: number;
  /** こちらが取りに行く間隔（秒）。検知の遅れの上限。 */
  readonly poll_interval_s: number;
};

/**
 * 観測が途切れてから「古い」とみなすまでの秒数。
 *
 * `greatest(expected * 3, poll * 3) + poll` は 0013 の式そのまま。取得間隔のほうが
 * 長いフィードを将来足しても破綻しないように、両方の 3 倍の大きいほうを取る。
 */
export const staleAfterSeconds = (cadence: FeedCadence): number =>
  Math.max(cadence.expected_cadence_s, cadence.poll_interval_s) * STALE_CADENCE_MULTIPLIER +
  cadence.poll_interval_s;

/**
 * 最後の観測から見て古いか。**未観測（null）は古い扱い**にする。
 * 「まだ一度も取れていない」を「新しい」と言ってはいけない。
 */
export const isStale = (params: {
  readonly cadence: FeedCadence;
  readonly last_observed_at: Date | null;
  readonly now: Date;
}): boolean => {
  if (params.last_observed_at === null) {
    return true;
  }
  const age_s = (params.now.getTime() - params.last_observed_at.getTime()) / MS_PER_S;
  return age_s > staleAfterSeconds(params.cadence);
};

/**
 * 予測をまだ「現在の予測」として出せるか（W4 プラン §4 の W4-01）。
 *
 * **測るのは `generated_at` ではなく `base_observed_at`。** 利用者に効くのは「いつ計算
 * したか」ではなく「**どの観測に基づくか**」で、現在値の `stale` 判定と同じ物差しに
 * なる。「台数は現在値なのに予測だけ古い」という食い違いが起きない。
 *
 * 閾値の `FORECAST_STALE_AFTER_S`（900 秒）は **`app_config.infer_alert_s` と同じ値**に
 * してある（migration 0029）。**利用者に出せなくなった瞬間に、監視も鳴る。**
 *
 * **フィードごとの `stale_after_s` は使わない。** ドコモは 303 秒で推論周期（300 秒）と
 * ほぼ同じになり、正常運転でも切れたり戻ったりする。
 *
 * **未計算（null）は「古い」扱い。** 「まだ一度も出ていない」を「新しい」と言わない
 * （`isStale` と同じ考え方）。
 */
export const isForecastFresh = (params: {
  readonly base_observed_at: Date | null;
  readonly now: Date;
}): boolean => {
  if (params.base_observed_at === null) {
    return false;
  }
  const age_s = (params.now.getTime() - params.base_observed_at.getTime()) / MS_PER_S;
  return age_s <= FORECAST_STALE_AFTER_S;
};
