/**
 * 公開 API `/v1` が読む DB の入り口（W2 プラン §5.7）。
 *
 * **触れてよいのは `v1_` で始まるビューだけ。** 基底テーブルには匿名の権限を与えておらず
 * （migration 0018、pgTAP 0010）、ここも同じ境界の内側で動く。ビューが `-1` を NULL に
 * 開き、壊れた座標と停止中システムを落としているので、上位は素直に写すだけでよい。
 *
 * 収集側の `IngestPort` / `WeatherPort` と同じ考え方で、DB とのやりとりを 2 つの関数に絞る。
 * **何をどう絞るかは `view-query.ts`（純粋）にあり、ここは supabase-js に渡すだけ。**
 *
 * 応答の行は **Zod で検査してから**使う。ビューの列を変えたのにこちらを直し忘れたとき、
 * 型だけでは気づけない（実行時には何でも入り得る）。
 */
import { V1_QUERY_TIMEOUT_MS, type Bbox, type SystemId } from "@bikechance/shared";
import type { SupabaseClient } from "@supabase/supabase-js";
import { z } from "zod";
import {
  CELLS_FUNCTION,
  FEEDS_VIEW,
  FEED_COLUMNS,
  HOURLY_COLUMNS,
  HOURLY_VIEW,
  NEIGHBORS_VIEW,
  NEIGHBOR_COLUMNS,
  STATIONS_VIEW,
  STATION_COLUMNS,
  STATION_ORDER,
  bboxFilters,
  feedRowSchema,
  hourlyFilters,
  cellRowSchema,
  hourlyRowSchema,
  neighborFilters,
  neighborRowSchema,
  oneStationFilters,
  stationIdFilters,
  stationRowSchema,
  systemFilters,
  type CellRow,
  type FeedRow,
  type Filter,
  type HourlyRow,
  type NeighborRow,
  type StationRow,
} from "./view-query";

export type { CellRow, FeedRow, HourlyRow, NeighborRow, StationRow } from "./view-query";

export type StationsPage = {
  readonly rows: readonly StationRow[];
  /** 上限を掛ける前の該当件数。「どれだけ狭めればよいか」を返すために使う。 */
  readonly total: number;
};

export type ReadPort = {
  readonly listFeeds: () => Promise<readonly FeedRow[]>;
  readonly listStationsInBbox: (params: {
    readonly bbox: Bbox;
    readonly system_id: SystemId | null;
    readonly limit: number;
  }) => Promise<StationsPage>;
  /**
   * ID を並べてポートを引く（`/v1/trip-check`）。**系統では絞らない**（W4-21）。
   * `station_id` はシステムをまたいで衝突するので、選り分けるのは呼ぶ側の仕事になる。
   */
  readonly listStationsByIds: (params: {
    readonly station_ids: readonly string[];
  }) => Promise<readonly StationRow[]>;
  /** 代替候補（同一システム・指定の半径まで）。**距離の昇順**で返す。 */
  readonly listNeighbors: (params: {
    readonly system_id: SystemId;
    readonly station_ids: readonly string[];
    readonly radius_m: number;
  }) => Promise<readonly NeighborRow[]>;
  /**
   * 1 ポートを名指しで引く（`/v1/stations/{system}/{station_id}`）。
   *
   * **無ければ null。** 「知らないポート」と「いま応えられない」を呼ぶ側が区別できる
   * ように、例外にしない（例外は `ReadError` で 503 に写る）。
   */
  readonly findStation: (params: {
    readonly system_id: SystemId;
    readonly station_id: string;
  }) => Promise<StationRow | null>;
  /**
   * 低ズームの格子集約（0049、W5 の PR E）。
   *
   * **集約は DB でやる。** 1,000 件の上限を超えた行をここまで持ってくると、上限を
   * 置いた意味が無い（0.5 度の矩形は 8,805 ポート）。
   *
   * **鮮度の閾値と水平の並びは呼ぶ側が渡す。** どちらも規則の正が `packages/shared`
   * にあり、SQL で書き直すと 2 か所になる。
   */
  readonly listCells: (params: {
    readonly bbox: Bbox;
    readonly cell_deg: number;
    readonly system_id: SystemId | null;
    /** これより古い観測に基づく予測は、確率の集約に入れない。 */
    readonly fresh_after: Date;
    /** この並びと一致する行だけが確率に寄与する（添字のずれを作らない）。 */
    readonly horizons_min: readonly number[];
  }) => Promise<readonly CellRow[]>;
  /** 直近の実績（1 時間ごと）。**古い順**で返す。 */
  readonly listRecentHours: (params: {
    readonly system_id: SystemId;
    readonly station_id: string;
    readonly since: Date;
  }) => Promise<readonly HourlyRow[]>;
};

/** DB に届かなかった／応答の形が違った。上位はこれを 503 に写す。 */
export class ReadError extends Error {
  constructor(relation: string, message: string) {
    super(`${relation}: ${message}`);
    this.name = "ReadError";
  }
}

/** 絞り込みを順に適用する。どのメソッドも同じビルダを返す。 */
type Filterable<T> = {
  // 時刻の下限も同じメソッドに写る。**分けているのは組み立てる側の型だけ**で、
  // 数と時刻を取り違えたまま通ることを `Filter` の側で止めている
  readonly gte: (column: string, value: number | string) => T;
  readonly lte: (column: string, value: number) => T;
  readonly eq: (column: string, value: string) => T;
  readonly is: (column: string, value: boolean) => T;
  readonly in: (column: string, values: readonly string[]) => T;
};

const applyFilters = <T extends Filterable<T>>(builder: T, filters: readonly Filter[]): T =>
  filters.reduce<T>((acc, filter) => {
    switch (filter.op) {
      case "gte":
      case "gte_text":
        return acc.gte(filter.column, filter.value);
      case "lte":
        return acc.lte(filter.column, filter.value);
      case "eq":
        return acc.eq(filter.column, filter.value);
      // 真偽値は `eq` ではなく `is`。PostgREST は `eq.true` も解するが、**NULL を含む列で
      // 意味が変わる**（`is` は三値論理を正しく扱う）ので、真偽値は常に `is` で送る
      case "is":
        return acc.is(filter.column, filter.value);
      case "in":
        return acc.in(filter.column, filter.values);
    }
  }, builder);

const parseRows = <T>(relation: string, schema: z.ZodType<T>, data: unknown): readonly T[] => {
  const parsed = z.array(schema).safeParse(data);
  if (parsed.success) {
    return parsed.data;
  }
  // どこが違うかだけを残す。行の中身は載せない
  const issue = parsed.error.issues[0];
  throw new ReadError(relation, `応答の形が違います（${issue?.path.join(".") ?? "?"}）`);
};

const toReadError = (relation: string, cause: unknown): ReadError =>
  new ReadError(relation, cause instanceof Error ? cause.message : String(cause));

export const createSupabaseReadPort = (client: SupabaseClient): ReadPort => ({
  listFeeds: async () => {
    const { data, error } = await client
      .from(FEEDS_VIEW)
      .select(FEED_COLUMNS)
      .abortSignal(AbortSignal.timeout(V1_QUERY_TIMEOUT_MS));
    if (error !== null) {
      throw toReadError(FEEDS_VIEW, error);
    }
    return parseRows(FEEDS_VIEW, feedRowSchema, data);
  },

  listStationsInBbox: async ({ bbox, system_id, limit }) => {
    // 件数（count: exact）も一緒に取る。上限を超えたことを、切り捨てる前に知りたい
    const selected = client.from(STATIONS_VIEW).select(STATION_COLUMNS, { count: "exact" });
    const filtered = applyFilters(selected, [...bboxFilters(bbox), ...systemFilters(system_id)]);
    const ordered = STATION_ORDER.reduce(
      (acc, column) => acc.order(column, { ascending: true }),
      filtered,
    );
    const { data, error, count } = await ordered
      .limit(limit)
      .abortSignal(AbortSignal.timeout(V1_QUERY_TIMEOUT_MS));
    if (error !== null) {
      throw toReadError(STATIONS_VIEW, error);
    }
    const rows = parseRows(STATIONS_VIEW, stationRowSchema, data);
    return { rows, total: count ?? rows.length };
  },

  listStationsByIds: async ({ station_ids }) => {
    // **空の `in` は送らない。** PostgREST は `in.()` を構文誤りとして 400 にする
    if (station_ids.length === 0) {
      return [];
    }
    const selected = client.from(STATIONS_VIEW).select(STATION_COLUMNS);
    const { data, error } = await applyFilters(selected, stationIdFilters(station_ids)).abortSignal(
      AbortSignal.timeout(V1_QUERY_TIMEOUT_MS),
    );
    if (error !== null) {
      throw toReadError(STATIONS_VIEW, error);
    }
    return parseRows(STATIONS_VIEW, stationRowSchema, data);
  },

  findStation: async ({ system_id, station_id }) => {
    const selected = client.from(STATIONS_VIEW).select(STATION_COLUMNS);
    const { data, error } = await applyFilters(
      selected,
      oneStationFilters({ system_id, station_id }),
    )
      // **2 行返ることは無い**（ビューの元は 1 ポート 1 行）が、上限を置いておけば
      // 万一そうなっても応答が膨らまない
      .limit(2)
      .abortSignal(AbortSignal.timeout(V1_QUERY_TIMEOUT_MS));
    if (error !== null) {
      throw toReadError(STATIONS_VIEW, error);
    }
    const rows = parseRows(STATIONS_VIEW, stationRowSchema, data);
    return rows[0] ?? null;
  },

  listCells: async ({ bbox, cell_deg, system_id, fresh_after, horizons_min }) => {
    // **関数を呼ぶ。** ビューでは刻みを受けられない（同じビューに 2 つの粒度を持たせると、
    // 呼ぶ側がどちらを読んでいるか分からなくなる）
    const { data, error } = await client
      .rpc(CELLS_FUNCTION, {
        p_west: bbox.west,
        p_south: bbox.south,
        p_east: bbox.east,
        p_north: bbox.north,
        p_cell_deg: cell_deg,
        p_system: system_id,
        p_fresh_after: fresh_after.toISOString(),
        p_horizons: [...horizons_min],
      })
      .abortSignal(AbortSignal.timeout(V1_QUERY_TIMEOUT_MS));
    if (error !== null) {
      throw toReadError(CELLS_FUNCTION, error);
    }
    return parseRows(CELLS_FUNCTION, cellRowSchema, data);
  },

  listRecentHours: async ({ system_id, station_id, since }) => {
    const selected = client.from(HOURLY_VIEW).select(HOURLY_COLUMNS);
    const { data, error } = await applyFilters(
      selected,
      hourlyFilters({ system_id, station_id, since: since.toISOString() }),
    )
      // **古い順。** グラフは左から右に時間が流れる
      .order("hour_start", { ascending: true })
      .abortSignal(AbortSignal.timeout(V1_QUERY_TIMEOUT_MS));
    if (error !== null) {
      throw toReadError(HOURLY_VIEW, error);
    }
    return parseRows(HOURLY_VIEW, hourlyRowSchema, data);
  },

  listNeighbors: async ({ system_id, station_ids, radius_m }) => {
    if (station_ids.length === 0) {
      return [];
    }
    const selected = client.from(NEIGHBORS_VIEW).select(NEIGHBOR_COLUMNS);
    // 並びは固定する。同じ要求が同じ応答になり、差分も追える（`STATION_ORDER` と同じ方針）
    const { data, error } = await applyFilters(
      selected,
      neighborFilters({ system_id, station_ids, radius_m }),
    )
      .order("distance_m", { ascending: true })
      .order("nb_station_id", { ascending: true })
      .abortSignal(AbortSignal.timeout(V1_QUERY_TIMEOUT_MS));
    if (error !== null) {
      throw toReadError(NEIGHBORS_VIEW, error);
    }
    return parseRows(NEIGHBORS_VIEW, neighborRowSchema, data);
  },
});
