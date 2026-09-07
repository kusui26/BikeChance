/**
 * `/v1/meta` の組み立て（W2 プラン §5.7、開発プラン §8.3）。
 *
 * **DB が落ちても 200 を返す。** この経路の役目は「いまデータがどういう状態か」を
 * 伝えることなので、伝えられないときこそ「分からない（＝古い）」と返す必要がある。
 * 落として 500 にすると、クライアントは鮮度も表示条件も判断できなくなる。
 */
import {
  ATTRIBUTIONS,
  FORECAST_DISCLAIMER,
  SYSTEMS,
  SYSTEM_IDS,
  buildOdptNotice,
  isStale,
  metaResponseSchema,
  staleAfterSeconds,
  type FeedStatus,
  type MetaResponse,
} from "@bikechance/shared";
import type { FeedRow } from "./read-port";

const FALLBACK_CONTACT_EMAIL = "contact@example.com";

/** DB から鮮度を取れなかったときの姿。**未観測は古い扱い**にする。 */
const unknownFeeds = (): readonly FeedStatus[] =>
  SYSTEM_IDS.map((system_id) => ({
    system_id,
    display_name: SYSTEMS[system_id].display_name,
    data_updated_at: null,
    expected_cadence_s: SYSTEMS[system_id].expected_cadence_s,
    stale_after_s: staleAfterSeconds(SYSTEMS[system_id]),
    stale: true,
    capacity_is_dynamic: SYSTEMS[system_id].capacity_is_dynamic,
  }));

const toFeedStatus = (feed: FeedRow, now: Date): FeedStatus => {
  const last_observed_at = feed.last_observed_at === null ? null : new Date(feed.last_observed_at);
  return {
    system_id: feed.system_id,
    display_name: feed.display_name,
    data_updated_at: last_observed_at?.toISOString() ?? null,
    expected_cadence_s: feed.expected_cadence_s,
    stale_after_s: staleAfterSeconds(feed),
    stale: isStale({ cadence: feed, last_observed_at, now }),
    capacity_is_dynamic: feed.capacity_is_dynamic,
  };
};

export const buildMeta = (params: {
  /** DB から取れた鮮度。取れなかったときは null を渡す。 */
  readonly feeds: readonly FeedRow[] | null;
  readonly now: Date;
  readonly contact_email: string | undefined;
}): MetaResponse => {
  const feeds =
    params.feeds === null
      ? unknownFeeds()
      : params.feeds.map((feed) => toFeedStatus(feed, params.now));
  return metaResponseSchema.parse({
    api_version: "v1",
    generated_at: params.now.toISOString(),
    // 予測の有無ではなく**データの鮮度**を表す。予測は model_version を見る
    stale: feeds.some((feed) => feed.stale),
    model_version: null,
    feeds,
    attribution: ATTRIBUTIONS,
    notice: buildOdptNotice(params.contact_email ?? FALLBACK_CONTACT_EMAIL),
    disclaimer: FORECAST_DISCLAIMER,
  });
};
