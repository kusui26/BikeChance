/**
 * `/v1/stations/{system}/{station_id}` の組み立て（W5 プラン §6.6 の PR F）。
 *
 * **`/v1/stations` と同じ部品を通る。** ポートの写し方は `station-row.ts`、鮮度の規則は
 * `@bikechance/shared` の `freshness.ts` にある。ここが持つのは**この経路だけの判断**で、
 * それは 3 つしかない。
 *
 *   1. **経路の系統と ID を検証してから DB に触る**（誤りは常に 400 で返す）
 *   2. **知らないポートは 404**。`ok: false` で返し、例外にしない
 *   3. **直近 24 時間は「読む側が切る」**——ビューは 26 時間持っている（0046）
 *
 * **`at` を受けない。** 曲線を返すので到着時刻が要らず、受けると同じポートの URL が
 * 5 分ごとに割れて CDN が効かない（W5 プラン §6.6）。
 */
import {
  ATTRIBUTIONS,
  SYSTEM_IDS,
  isStale,
  staleAfterSeconds,
  stationDetailResponseSchema,
  type FeedStatus,
  type StationDetailResponse,
  type SystemId,
} from "@bikechance/shared";
import type { ProblemCode } from "./problem";
import type { FeedRow, HourlyRow, ReadPort, StationRow } from "./read-port";
import { hasLocation, toForecastCurve, toRecentHours, toStation } from "./station-row";

/** 直近の実績を返す幅。**ビューは 26 時間持っている**ので、切るのはここ（W4-26 と同じ方針）。 */
export const RECENT_HOURS = 24;

const MS_PER_HOUR = 3_600_000;

export type StationDetailFailure = {
  readonly code: ProblemCode;
  readonly status: number;
  readonly detail: string;
};

export type StationDetailOutcome =
  | { readonly ok: true; readonly response: StationDetailResponse }
  | { readonly ok: false; readonly failure: StationDetailFailure };

const failure = (code: ProblemCode, status: number, detail: string): StationDetailOutcome => ({
  ok: false,
  failure: { code, status, detail },
});

const isSystemId = (value: string): value is SystemId => SYSTEM_IDS.some((id) => id === value);

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

export const buildStationDetailResponse = (params: {
  readonly row: StationRow;
  readonly feed: FeedRow;
  readonly recent: readonly HourlyRow[];
  readonly now: Date;
}): StationDetailResponse | null => {
  // **座標の無いポートは詳細も返さない。** `StationCurrent` は座標を必須にしており、
  // 地図に出ないポートの詳細を URL から開ける状態にしない（W5-15 と同じ判断）
  if (!hasLocation(params.row)) {
    return null;
  }
  const feed = toFeedStatus(params.feed, params.now);
  return stationDetailResponseSchema.parse({
    api_version: "v1",
    generated_at: params.now.toISOString(),
    stale: feed.stale,
    // **地図と同じ写し方を通す**（`forecast` は詳細では使わないので null）
    station: toStation(params.row, params.row.is_present ? feed.data_updated_at : null, null),
    forecast_curve: toForecastCurve(params.row, params.now),
    recent: toRecentHours(params.recent),
    // **そのポートのシステムだけ。** 地図と違い 1 系統しか関係しない
    feeds: [feed],
    attribution: ATTRIBUTIONS.filter((one) => one.system_id === params.row.system_id),
  });
};

/**
 * 経路を検証してから DB に触る。
 *
 * ポートを**関数で受け取る**のは `queryStations` と同じ順序のため。設定が足りない環境で
 * 経路の誤りを 503 と報告すると、呼び出し側は自分の誤りに気づけない。
 */
export const queryStationDetail = async (params: {
  readonly makePort: () => ReadPort;
  readonly system: string;
  readonly station_id: string;
  readonly now: Date;
}): Promise<StationDetailOutcome> => {
  if (!isSystemId(params.system)) {
    return failure("unknown_system", 400, `system は ${SYSTEM_IDS.join(" か ")} です。`);
  }
  if (params.station_id === "") {
    return failure("station_not_found", 404, "そのポートは見つかりません。");
  }
  const system_id = params.system;
  try {
    const port = params.makePort();
    const since = new Date(params.now.getTime() - RECENT_HOURS * MS_PER_HOUR);
    const [feeds, row, recent] = await Promise.all([
      port.listFeeds(),
      port.findStation({ system_id, station_id: params.station_id }),
      port.listRecentHours({ system_id, station_id: params.station_id, since }),
    ]);
    const feed = feeds.find((one) => one.system_id === system_id);
    // **フィードが無いのは「そのシステムが停止中」**（`v1_feeds` は停止中を出さない）。
    // ポートの側も同じ約束でビューから落ちているので、見つからないと答えるのが正しい
    if (row === null || feed === undefined) {
      return failure("station_not_found", 404, "そのポートは見つかりません。");
    }
    const response = buildStationDetailResponse({ row, feed, recent, now: params.now });
    return response === null
      ? failure("station_not_found", 404, "そのポートは見つかりません。")
      : { ok: true, response };
    // 設定の欠落（環境変数）も DB の不調も、呼び出し側から見れば「いま応えられない」
  } catch {
    return failure("upstream_unavailable", 503, "しばらくしてから再試行してください。");
  }
};
