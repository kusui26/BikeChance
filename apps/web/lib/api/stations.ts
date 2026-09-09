/**
 * `/v1/stations` の組み立て（W2 プラン §5.7）。
 *
 * ここは**手順と写像だけ**を持つ。DB とのやりとりは `read-port.ts`、bbox の規則は
 * `@bikechance/shared` の `bbox.ts`、鮮度の規則は同じく `freshness.ts` にある。
 *
 * 設計の要点：
 *   * 想定内の入力誤りは例外にしない。`ok: false` と Problem の材料を返す
 *   * **上限を超えたら切り捨てずに 400**。穴の開いた地図を黙って返さない
 *   * 値には必ず観測時刻を添える。不在のポートは `observed_at` を null にする（§2 の 7）
 */
import {
  ATTRIBUTIONS,
  STATIONS_MAX_RESULTS,
  SYSTEM_IDS,
  interpolateForecast,
  isForecastFresh,
  isStale,
  parseArrival,
  parseBbox,
  quantizeBbox,
  staleAfterSeconds,
  stationsResponseSchema,
  type Bbox,
  type FeedStatus,
  type StationCurrent,
  type StationForecast,
  type StationsResponse,
  type SystemId,
} from "@bikechance/shared";
import type { ProblemCode } from "./problem";
import type { FeedRow, ReadPort, StationRow } from "./read-port";

export type StationsFailure = {
  readonly code: ProblemCode;
  readonly status: number;
  readonly detail: string;
};

export type StationsOutcome =
  | { readonly ok: true; readonly response: StationsResponse }
  | { readonly ok: false; readonly failure: StationsFailure };

const badRequest = (code: ProblemCode, detail: string): StationsOutcome => ({
  ok: false,
  failure: { code, status: 400, detail },
});

/** `parseBbox` の失敗の種類を、そのまま Problem の code に写す。 */
const BBOX_PROBLEM_CODES = {
  missing: "bbox_missing",
  malformed: "bbox_malformed",
  out_of_range: "bbox_out_of_range",
  inverted: "bbox_inverted",
  too_large: "bbox_too_large",
} as const satisfies Readonly<Record<string, ProblemCode>>;

const isSystemId = (value: string): value is SystemId => SYSTEM_IDS.some((id) => id === value);

/** `?system=` の検証。未指定は「全システム」。 */
const readSystem = (
  value: string | null,
): { readonly ok: true; readonly system_id: SystemId | null } | { readonly ok: false } => {
  if (value === null || value === "") {
    return { ok: true, system_id: null };
  }
  return isSystemId(value) ? { ok: true, system_id: value } : { ok: false };
};

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

/**
 * 行を応答の形に写す。
 *
 * `observed_at` に入れるのは**フィードの観測時刻**であって `last_changed_at` ではない。
 * 後者は「最後に値が変わった時刻」で、変わっていないだけの値を古く見せてしまう（§11.3）。
 * ポートが最新のフィードに現れていない（`is_present` が false）なら、その値がいつのものかは
 * 分からないので null にする。
 */
const toStation = (
  row: StationRow,
  observed_at: string | null,
  forecast: StationForecast | null,
): StationCurrent => ({
  system_id: row.system_id,
  station_id: row.station_id,
  name: row.name,
  lat: row.lat,
  lon: row.lon,
  capacity: row.capacity,
  bikes: row.bikes,
  docks: row.docks,
  is_installed: row.is_installed,
  is_renting: row.is_renting,
  is_returning: row.is_returning,
  is_present: row.is_present,
  observed_at: row.is_present ? observed_at : null,
  last_changed_at: new Date(row.last_changed_at).toISOString(),
  forecast,
});

/**
 * 1 ポートぶんの予測を組み立てる（W4 プラン §4 の W4-01・W4-02）。
 *
 * **null になる道が 4 つある。** どれも「出せない」として同じ形で返し、理由は返さない。
 *   1. 要求に `at` / `in_min` が無い（`in_min` が null）
 *   2. そのポートの予測がまだ無い（貸出も返却も止まっている・成果物に無い）
 *   3. **基づく観測が古い**（`FORECAST_STALE_AFTER_S` を超えた）
 *   4. 配列の長さがそろっていない（欠けた表から数を作らない）
 *
 * **3 を `generated_at` ではなく `base_observed_at` で測る**のが要点。利用者に効くのは
 * 「どの観測に基づくか」で、現在値の `stale` 判定と同じ物差しになる。
 */
const toForecast = (row: StationRow, in_min: number | null, now: Date): StationForecast | null => {
  if (in_min === null) {
    return null;
  }
  const base = row.forecast_base_observed_at;
  if (base === null || row.forecast_confidence === null || row.forecast_model_version === null) {
    return null;
  }
  const base_observed_at = new Date(base);
  if (!isForecastFresh({ base_observed_at, now })) {
    return null;
  }
  const p_bike = interpolateForecast({
    horizons_min: row.forecast_horizons_min,
    values_x1000: row.forecast_p_bike_x1000,
    in_min,
  });
  const p_dock = interpolateForecast({
    horizons_min: row.forecast_horizons_min,
    values_x1000: row.forecast_p_dock_x1000,
    in_min,
  });
  // **片方だけ返さない。** borrow と return はどちらも表示に要る
  if (p_bike === null || p_dock === null) {
    return null;
  }
  return {
    p_bike,
    p_dock,
    confidence: row.forecast_confidence,
    base_observed_at: base_observed_at.toISOString(),
    model_version: row.forecast_model_version,
  };
};

export const buildStationsResponse = (params: {
  readonly bbox: Bbox;
  readonly feeds: readonly FeedRow[];
  readonly rows: readonly StationRow[];
  /** 何分先の予測を出すか。**5 分に丸めた後**の値。指定が無ければ null。 */
  readonly in_min: number | null;
  readonly now: Date;
}): StationsResponse => {
  const feeds = params.feeds.map((feed) => toFeedStatus(feed, params.now));
  const observedBySystem = new Map(feeds.map((feed) => [feed.system_id, feed.data_updated_at]));
  const stations = params.rows.map((row) =>
    toStation(
      row,
      observedBySystem.get(row.system_id) ?? null,
      toForecast(row, params.in_min, params.now),
    ),
  );
  return stationsResponseSchema.parse({
    api_version: "v1",
    generated_at: params.now.toISOString(),
    bbox: params.bbox,
    count: stations.length,
    stale: feeds.some((feed) => feed.stale),
    forecast_in_min: params.in_min,
    feeds,
    stations,
    attribution: ATTRIBUTIONS,
  });
};

/**
 * `bbox` を検証してから DB に触る。
 *
 * ポートを**関数で受け取る**のは順序のため。設定が足りない環境で bbox の誤りを 503 と
 * 報告してしまうと、呼び出し側は自分の誤りに気づけない。入力の誤りは常に 400 で返す。
 */
export const queryStations = async (params: {
  readonly makePort: () => ReadPort;
  readonly search: URLSearchParams;
  readonly now: Date;
}): Promise<StationsOutcome> => {
  const parsed = parseBbox(params.search.get("bbox"));
  if (!parsed.ok) {
    return badRequest(BBOX_PROBLEM_CODES[parsed.problem], parsed.detail);
  }
  const system = readSystem(params.search.get("system"));
  if (!system.ok) {
    return badRequest("unknown_system", `system は ${SYSTEM_IDS.join(" か ")} です。`);
  }
  // 到着の指定も **DB に触る前に**検証する。入力の誤りは常に 400 で返す
  const arrival = parseArrival({
    at: params.search.get("at"),
    in_min: params.search.get("in_min"),
    now: params.now,
  });
  if (!arrival.ok) {
    return badRequest(arrival.problem, arrival.detail);
  }

  // 格子に外側へ丸めてから問い合わせる。細かい位置は問い合わせにも記録にも残らない
  const bbox = quantizeBbox(parsed.bbox);
  try {
    const port = params.makePort();
    const [feeds, page] = await Promise.all([
      port.listFeeds(),
      port.listStationsInBbox({
        bbox,
        system_id: system.system_id,
        limit: STATIONS_MAX_RESULTS,
      }),
    ]);
    if (page.total > STATIONS_MAX_RESULTS) {
      return badRequest(
        "too_many_stations",
        `${page.total} 件が該当しました（上限 ${STATIONS_MAX_RESULTS} 件）。bbox を狭めてください。`,
      );
    }
    return {
      ok: true,
      response: buildStationsResponse({
        bbox,
        feeds,
        rows: page.rows,
        in_min: arrival.in_min,
        now: params.now,
      }),
    };
    // 設定の欠落（環境変数）も DB の不調も、呼び出し側から見れば「いま応えられない」。
    // 理由は応答に載せない（設定の内容が漏れる経路を作らない）
  } catch {
    return {
      ok: false,
      failure: {
        code: "upstream_unavailable",
        status: 503,
        detail: "しばらくしてから再試行してください。",
      },
    };
  }
};
