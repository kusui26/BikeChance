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
  FORECAST_STALE_AFTER_S,
  HORIZONS_MIN,
  STATIONS_MAX_RESULTS,
  SYSTEM_IDS,
  isStale,
  parseArrival,
  parseBbox,
  planCells,
  quantizeBbox,
  staleAfterSeconds,
  stationCellsResponseSchema,
  stationsResponseSchema,
  type Bbox,
  type CellPlan,
  type FeedStatus,
  type StationCellsResponse,
  type StationsEndpointResponse,
  type StationsResponse,
  type SystemId,
} from "@bikechance/shared";
import type { ProblemCode } from "./problem";
import type { CellRow, FeedRow, ReadPort, StationRow } from "./read-port";
import { toCell } from "./station-cell";
import { hasLocation, toForecast, toStation } from "./station-row";

export type StationsFailure = {
  readonly code: ProblemCode;
  readonly status: number;
  readonly detail: string;
};

export type StationsOutcome =
  | { readonly ok: true; readonly response: StationsEndpointResponse }
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
  // **座標の無い行は外す。** bbox で引いているので範囲比較で自然に落ちており、実際には
  // 1 件も通らない——が、**「落ちているはず」を型で言える形にしておく**（`hasLocation`）。
  // ビューは座標の無いポートも持っている（実測 10 件。`view-query.ts`）
  const stations = params.rows
    .filter(hasLocation)
    .map((row) =>
      toStation(
        row,
        observedBySystem.get(row.system_id) ?? null,
        toForecast(row, params.in_min, params.now),
      ),
    );
  return stationsResponseSchema.parse({
    api_version: "v1",
    generated_at: params.now.toISOString(),
    aggregation: "station",
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
 * 低ズームの応答を組み立てる（W5 の PR E）。
 *
 * **`stations` は入らない。** 同じ応答に 2 つの粒度を混ぜると、読む側が「どちらを
 * 信じるか」を決めることになる（W5-12）。
 */
export const buildCellsResponse = (params: {
  readonly bbox: Bbox;
  readonly cell_deg: number;
  readonly feeds: readonly FeedRow[];
  readonly rows: readonly CellRow[];
  readonly in_min: number | null;
  readonly now: Date;
}): StationCellsResponse => {
  const feeds = params.feeds.map((feed) => toFeedStatus(feed, params.now));
  const cells = params.rows.map((row) => toCell(row, params.in_min, params.now));
  return stationCellsResponseSchema.parse({
    api_version: "v1",
    generated_at: params.now.toISOString(),
    aggregation: "cell",
    bbox: params.bbox,
    cell_deg: params.cell_deg,
    count: cells.length,
    // **フィードの鮮度はポートのときと同じ**。セルごとの `stale` とは別の話で、
    // あちらは「値をいつのものと言えないポートが混ざっている」を指す
    stale: feeds.some((feed) => feed.stale),
    forecast_in_min: params.in_min,
    feeds,
    cells,
    attribution: ATTRIBUTIONS,
  });
};

/** 上限を超えたら切り捨てずに 400。**穴の開いた地図を黙って返さない。** */
const tooMany = (total: number): StationsOutcome =>
  badRequest(
    "too_many_stations",
    `${total} 件が該当しました（上限 ${STATIONS_MAX_RESULTS} 件）。bbox を狭めてください。`,
  );

/**
 * セルを引いて組み立てる。
 *
 * **上限の判定はここにも置く。** 刻みの決め方（`planCells`）からは、0.25 度で
 * 25 × 25 ＝ 625、0.5 度で 10 × 10 ＝ 100 が最大で、**1,000 を超える道は無い**。
 * それでも消さないのは、**刻みの表を変えたときに黙って超えないようにする**ため——
 * 規則を「いまは起きないから」で外すと、次に起きたときに誰も気づかない。
 */
const queryCells = async (params: {
  readonly port: ReadPort;
  readonly bbox: Bbox;
  readonly plan: Extract<CellPlan, { aggregation: "cell" }>;
  readonly system_id: SystemId | null;
  readonly in_min: number | null;
  readonly now: Date;
}): Promise<StationsOutcome> => {
  const [feeds, rows] = await Promise.all([
    params.port.listFeeds(),
    params.port.listCells({
      bbox: params.bbox,
      cell_deg: params.plan.cell_deg,
      system_id: params.system_id,
      // **鮮度の閾値はここで作る。** 規則の正は `packages/shared` で、SQL は比較するだけ
      fresh_after: new Date(params.now.getTime() - FORECAST_STALE_AFTER_S * 1000),
      horizons_min: HORIZONS_MIN,
    }),
  ]);
  if (rows.length > STATIONS_MAX_RESULTS) {
    return tooMany(rows.length);
  }
  return {
    ok: true,
    response: buildCellsResponse({
      bbox: params.bbox,
      cell_deg: params.plan.cell_deg,
      feeds,
      rows,
      in_min: params.in_min,
      now: params.now,
    }),
  };
};

/** ポートをそのまま引いて組み立てる（いままでどおり）。 */
const queryPorts = async (params: {
  readonly port: ReadPort;
  readonly bbox: Bbox;
  readonly system_id: SystemId | null;
  readonly in_min: number | null;
  readonly now: Date;
}): Promise<StationsOutcome> => {
  const [feeds, page] = await Promise.all([
    params.port.listFeeds(),
    params.port.listStationsInBbox({
      bbox: params.bbox,
      system_id: params.system_id,
      limit: STATIONS_MAX_RESULTS,
    }),
  ]);
  if (page.total > STATIONS_MAX_RESULTS) {
    return tooMany(page.total);
  }
  return {
    ok: true,
    response: buildStationsResponse({
      bbox: params.bbox,
      feeds,
      rows: page.rows,
      in_min: params.in_min,
      now: params.now,
    }),
  };
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
  // **丸めた後の辺で決める。** 要求の辺で決めると、丸めで広がったぶんだけ
  // 「セルにすべき矩形をポートで返す」ことが起きる（`planCells` は実効矩形の話）
  const plan = planCells(bbox);
  const shared = { bbox, system_id: system.system_id, in_min: arrival.in_min, now: params.now };
  try {
    const port = params.makePort();
    return plan.aggregation === "cell"
      ? await queryCells({ ...shared, port, plan })
      : await queryPorts({ ...shared, port });
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
