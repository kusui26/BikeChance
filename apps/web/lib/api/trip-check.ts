/**
 * `/v1/trip-check` の組み立て（W4 プラン §6.7）。
 *
 * ここは**手順と写像だけ**を持つ。DB とのやりとりは `read-port.ts`、ポートの写し方は
 * `station-row.ts`（`/v1/stations` と共用）、行程の規則は `@bikechance/shared` の
 * `trip.ts` にある。
 *
 * **答えるのは 1 つの問い**：「この時刻に出発ポートで借りられて、乗った先で返せるか」。
 *
 * 設計の要点（W4-21〜26）：
 *   * **系統は 1 つだけ受ける。** 事業者をまたぐ行程は成立しないので、表現できなくする
 *   * **到着も範囲を見る。** 出発だけ見ると、180 分の確率を 210 分の答えとして返す
 *   * **`trip` は両端がそろったときだけ。** 掛け算の片側が欠けた値を作らない（契約 10）
 *   * **代替候補は確率の高い順。** 距離順だと 6 番目の 90% が 5 番目の 40% に隠れる
 */
import {
  ATTRIBUTIONS,
  SYSTEM_IDS,
  TRIP_ALTERNATIVES_MAX,
  TRIP_ALTERNATIVE_RADIUS_M,
  TRIP_INDEPENDENCE_NOTICE,
  estimateRideMinutes,
  isStale,
  parseArrival,
  parseRideMinutes,
  staleAfterSeconds,
  tripArrival,
  tripCheckResponseSchema,
  tripProbability,
  type FeedStatus,
  type Point,
  type StationCurrent,
  type SystemId,
  type TripAlternative,
  type TripCheckResponse,
  type TripOutcome,
} from "@bikechance/shared";
import type { ProblemCode } from "./problem";
import type { FeedRow, NeighborRow, ReadPort, StationRow } from "./read-port";
import { hasLocation, toForecast, toStation, type LocatedRow } from "./station-row";

export type TripFailure = {
  readonly code: ProblemCode;
  readonly status: number;
  readonly detail: string;
};

/** 断った理由。**`Got<T>` の false 側と共用する**ので、途中の関数も同じ形で返せる。 */
type Refused = { readonly ok: false; readonly failure: TripFailure };

/** 途中の結果。`ok: true` の中身の名前を `value` で統一し、絞り込みが効くようにする。 */
type Got<T> = { readonly ok: true; readonly value: T } | Refused;

export type TripOutcomeResult =
  { readonly ok: true; readonly response: TripCheckResponse } | Refused;

const refuse = (code: ProblemCode, detail: string): Refused => ({
  ok: false,
  failure: { code, status: 400, detail },
});

/** 出発の指定に使う引数名。`/v1/stations` は `at` / `in_min`、こちらは `depart_*`。 */
const DEPART_LABELS = { at: "depart_at", in_min: "depart_in_min" } as const;

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

// ── 入力を読む（DB に触る前に済ませる） ──────────────────────

/** 読み取った入力。DB に触る前にここまで決まっている。 */
export type Asked = {
  readonly system_id: SystemId;
  readonly from_id: string;
  readonly to_id: string;
  readonly depart_in_min: number;
  /** 指定が無ければ null（サーバーが概算する）。 */
  readonly ride_min: number | null;
};

/** `from` と `to`。**同じポートは断る**（借りて同じ場所に返す行程は問いになっていない）。 */
const readEndpoints = (search: URLSearchParams): Got<{ from_id: string; to_id: string }> => {
  const from_id = (search.get("from") ?? "").trim();
  const to_id = (search.get("to") ?? "").trim();
  if (from_id === "" || to_id === "") {
    return refuse("station_missing", "from と to にポート ID を指定してください。");
  }
  return from_id === to_id
    ? refuse("same_station", "from と to が同じポートです。")
    : { ok: true, value: { from_id, to_id } };
};

/** 出発時刻。`/v1/stations` と同じ規則（5 分に丸める・同時指定は断る）で読む。 */
const readDeparture = (search: URLSearchParams, now: Date): Got<number> => {
  const depart = parseArrival({
    at: search.get("depart_at"),
    in_min: search.get("depart_in_min"),
    now,
    labels: DEPART_LABELS,
  });
  if (!depart.ok) {
    return refuse(depart.problem, depart.detail);
  }
  return depart.in_min === null
    ? refuse("depart_missing", "depart_at か depart_in_min を指定してください。")
    : { ok: true, value: depart.in_min };
};

/** 引数を読む。**入力の誤りは常に 400**（DB の不調と混ぜない）。 */
export const readAsked = (search: URLSearchParams, now: Date): Got<Asked> => {
  const system = search.get("system");
  if (system === null || !isSystemId(system)) {
    return refuse("unknown_system", `system は ${SYSTEM_IDS.join(" か ")} です。`);
  }
  const endpoints = readEndpoints(search);
  if (!endpoints.ok) {
    return endpoints;
  }
  const depart = readDeparture(search, now);
  if (!depart.ok) {
    return depart;
  }
  const ride = parseRideMinutes(search.get("ride_min"));
  return ride.ok
    ? {
        ok: true,
        value: {
          system_id: system,
          ...endpoints.value,
          depart_in_min: depart.value,
          ride_min: ride.ride_min,
        },
      }
    : refuse(ride.problem, ride.detail);
};

// ── ポートを選り分ける ──────────────────────────────────────

/**
 * 要求されたポートを選り分ける（W4-21）。
 *
 * **系統で絞らずに引いてある**ので、「別の系統に在る ID を渡した」のか「どこにも無い」
 * のかを区別して答えられる。**書けなくすることと、診断を諦めることは別である。**
 */
export const pickStation = (params: {
  readonly rows: readonly StationRow[];
  readonly system_id: SystemId;
  readonly station_id: string;
  readonly label: string;
}): Got<LocatedRow> => {
  const found = params.rows.find(
    (row) => row.system_id === params.system_id && row.station_id === params.station_id,
  );
  if (found !== undefined) {
    return hasLocation(found)
      ? { ok: true, value: found }
      : refuse(
          "station_location_missing",
          `${params.label} のポート ${params.station_id} は座標が登録されていないため、` +
            "行程の端点にできません。",
        );
  }
  const elsewhere = params.rows.find((row) => row.station_id === params.station_id);
  return refuse(
    "unknown_station",
    elsewhere === undefined
      ? `${params.label} のポート ${params.station_id} は ${params.system_id} にありません。`
      : `${params.label} のポート ${params.station_id} は ${params.system_id} ではなく ` +
          `${elsewhere.system_id} のポートです。`,
  );
};

// ── 乗車時間と到着 ──────────────────────────────────────────

export type Ride = { readonly ride_min: number; readonly estimated: boolean };

const asPoint = (row: LocatedRow): Point => ({ lat: row.lat, lon: row.lon });

/**
 * 乗車時間を決める（W4-23）。**省略されたら直線距離 ÷ 14 km/h で概算する。**
 *
 * 座標はここへ来る前にそろっている（`pickStation` が見ている）ので、**概算に失敗する
 * 道が無い**。
 */
export const decideRide = (asked: Asked, from: LocatedRow, to: LocatedRow): Ride =>
  asked.ride_min === null
    ? { ride_min: estimateRideMinutes(asPoint(from), asPoint(to)), estimated: true }
    : { ride_min: asked.ride_min, estimated: false };

/** 到着が水平の範囲に入っているか（W4-22）。外れたら 400。 */
const decideArrival = (depart_in_min: number, ride_min: number): Got<number> => {
  const arrival = tripArrival({ depart_in_min, ride_min });
  return arrival.ok
    ? { ok: true, value: arrival.arrive_in_min }
    : refuse("arrival_out_of_range", arrival.detail);
};

// ── 行程と代替候補 ──────────────────────────────────────────

/**
 * 行程が成立する確率（W4-24）。
 *
 * **両端がそろったときだけ返す。** `confidence` は小さいほう——鎖は弱い環の強さしかない。
 */
export const toTripOutcome = (from: StationCurrent, to: StationCurrent): TripOutcome | null => {
  if (from.forecast === null || to.forecast === null) {
    return null;
  }
  return {
    p_trip: tripProbability(from.forecast.p_bike, to.forecast.p_dock),
    confidence: Math.min(from.forecast.confidence, to.forecast.confidence),
    notice: TRIP_INDEPENDENCE_NOTICE,
  };
};

/** 代替候補で見る確率。出発側は「借りられるか」、到着側は「返せるか」。 */
export type Side = "bike" | "dock";

const chanceOf = (station: StationCurrent, side: Side): number | null => {
  if (station.forecast === null) {
    return null;
  }
  return side === "bike" ? station.forecast.p_bike : station.forecast.p_dock;
};

/** 確率の降順 → 距離の昇順 → ID の昇順。**同値でも並びが決まる**（同じ要求は同じ応答）。 */
const compareAlternatives = (left: TripAlternative, right: TripAlternative, side: Side): number => {
  const gap = (chanceOf(right.station, side) ?? 0) - (chanceOf(left.station, side) ?? 0);
  if (gap !== 0) {
    return gap;
  }
  return (
    left.distance_m - right.distance_m ||
    left.station.station_id.localeCompare(right.station.station_id)
  );
};

/**
 * 代替候補を並べる（W4-25）。**確率の高い順**、同値なら距離 → ID。
 *
 * **予測を出せないポートは入れない。** 確率を答えられない候補は問いに答えていない。
 * **「端点より良い」では絞らない**——出すかどうかは画面の判断で、API は材料を渡す。
 */
export const toAlternatives = (params: {
  readonly neighbors: readonly NeighborRow[];
  readonly stations: ReadonlyMap<string, StationCurrent>;
  readonly side: Side;
}): readonly TripAlternative[] =>
  params.neighbors
    .flatMap((neighbor) => {
      const station = params.stations.get(neighbor.nb_station_id);
      return station === undefined || chanceOf(station, params.side) === null
        ? []
        : [{ distance_m: neighbor.distance_m, station }];
    })
    .sort((left, right) => compareAlternatives(left, right, params.side))
    .slice(0, TRIP_ALTERNATIVES_MAX);

// ── 応答を組み立てる ────────────────────────────────────────

/** 端点と代替候補を、**それぞれの時刻**で写す。時刻が違うのがこの API の肝である。 */
type Mapped = {
  readonly station: StationCurrent;
  readonly alternatives: readonly TripAlternative[];
};

/**
 * 1 端点ぶんを組み立てる。
 *
 * **代替候補も端点と同じ時刻で評価する**（W4-25）。400 m の寄り道は 14 km/h で 1.7 分、
 * 出発の刻み（5 分）より細かいので、乗車時間を引き直す意味が無い。
 */
const mapEndpoint = (params: {
  readonly row: LocatedRow;
  readonly neighbors: readonly NeighborRow[];
  readonly neighborRows: readonly StationRow[];
  readonly observed_at: string | null;
  readonly in_min: number;
  readonly side: Side;
  readonly now: Date;
}): Mapped => {
  const toCurrent = (row: LocatedRow): StationCurrent =>
    toStation(row, params.observed_at, toForecast(row, params.in_min, params.now));
  // **座標の無い近傍は候補にしない。** 歩いて向かう先なので、地図に出せない候補は
  // 出しても行けない（端点と同じ理由で外す）
  const stations = new Map(
    params.neighborRows.filter(hasLocation).map((row) => [row.station_id, toCurrent(row)]),
  );
  return {
    station: toCurrent(params.row),
    alternatives: toAlternatives({ neighbors: params.neighbors, stations, side: params.side }),
  };
};

export const buildTripResponse = (params: {
  readonly asked: Asked;
  readonly ride: Ride;
  readonly arrive_in_min: number;
  readonly feeds: readonly FeedRow[];
  readonly from: LocatedRow;
  readonly to: LocatedRow;
  readonly neighbors: readonly NeighborRow[];
  readonly neighborRows: readonly StationRow[];
  readonly now: Date;
}): TripCheckResponse => {
  const feeds = params.feeds.map((feed) => toFeedStatus(feed, params.now));
  const observed_at =
    feeds.find((feed) => feed.system_id === params.asked.system_id)?.data_updated_at ?? null;
  const side = (station_id: string): readonly NeighborRow[] =>
    params.neighbors.filter((neighbor) => neighbor.station_id === station_id);
  const shared = { neighborRows: params.neighborRows, observed_at, now: params.now };
  const from = mapEndpoint({
    ...shared,
    row: params.from,
    neighbors: side(params.asked.from_id),
    in_min: params.asked.depart_in_min,
    side: "bike",
  });
  const to = mapEndpoint({
    ...shared,
    row: params.to,
    neighbors: side(params.asked.to_id),
    in_min: params.arrive_in_min,
    side: "dock",
  });
  return tripCheckResponseSchema.parse({
    api_version: "v1",
    generated_at: params.now.toISOString(),
    stale: feeds.some((feed) => feed.stale),
    system_id: params.asked.system_id,
    depart_in_min: params.asked.depart_in_min,
    ride_min: params.ride.ride_min,
    ride_min_estimated: params.ride.estimated,
    arrive_in_min: params.arrive_in_min,
    from: from.station,
    to: to.station,
    trip: toTripOutcome(from.station, to.station),
    alternatives: { from: from.alternatives, to: to.alternatives },
    feeds,
    attribution: ATTRIBUTIONS,
  });
};

// ── 実行 ────────────────────────────────────────────────────

/** DB から取った素材。検証が通ったあとで応答に写す。 */
type Fetched = {
  readonly feeds: readonly FeedRow[];
  readonly rows: readonly StationRow[];
  readonly neighbors: readonly NeighborRow[];
};

/**
 * 1 往復で取れるものをまとめて取る。
 *
 * 近傍も一緒に引くのは往復を減らすためで、**要求が誤っていても害が無い**——一致する
 * 行が無いだけで、例外にはならない。誤りは取ったあとの選り分けで 400 になる。
 */
const fetchAll = async (port: ReadPort, asked: Asked): Promise<Fetched> => {
  const station_ids = [asked.from_id, asked.to_id];
  const [feeds, rows, neighbors] = await Promise.all([
    port.listFeeds(),
    port.listStationsByIds({ station_ids }),
    port.listNeighbors({
      system_id: asked.system_id,
      station_ids,
      radius_m: TRIP_ALTERNATIVE_RADIUS_M,
    }),
  ]);
  return { feeds, rows, neighbors };
};

/** 近傍のポートを引く。**0 件なら問い合わせない**（空の `in` は PostgREST が断る）。 */
const fetchNeighborRows = async (
  port: ReadPort,
  neighbors: readonly NeighborRow[],
): Promise<readonly StationRow[]> => {
  const station_ids = [...new Set(neighbors.map((neighbor) => neighbor.nb_station_id))];
  return station_ids.length === 0 ? [] : port.listStationsByIds({ station_ids });
};

/** 検証が通った素材。**ここまで来れば 400 は無い。** */
type Checked = { readonly from: LocatedRow; readonly to: LocatedRow; readonly ride: Ride };

const checkFetched = (asked: Asked, fetched: Fetched): Got<Checked> => {
  const pick = (station_id: string, label: string): Got<LocatedRow> =>
    pickStation({ rows: fetched.rows, system_id: asked.system_id, station_id, label });
  const from = pick(asked.from_id, "from");
  if (!from.ok) {
    return from;
  }
  const to = pick(asked.to_id, "to");
  if (!to.ok) {
    return to;
  }
  return {
    ok: true,
    value: { from: from.value, to: to.value, ride: decideRide(asked, from.value, to.value) },
  };
};

/**
 * 行程チェックを 1 件組み立てる。
 *
 * **入力の検証を DB に触る前に済ませる**のは `/v1/stations` と同じ理由：設定が足りない
 * 環境で入力の誤りを 503 と報告すると、呼び出し側は自分の誤りに気づけない。
 * ポートの存在だけは DB を見ないと分からないので、取ったあとに 400 を返す。
 */
export const queryTrip = async (params: {
  readonly makePort: () => ReadPort;
  readonly search: URLSearchParams;
  readonly now: Date;
}): Promise<TripOutcomeResult> => {
  const asked = readAsked(params.search, params.now);
  if (!asked.ok) {
    return asked;
  }
  try {
    const port = params.makePort();
    const fetched = await fetchAll(port, asked.value);
    const checked = checkFetched(asked.value, fetched);
    if (!checked.ok) {
      return checked;
    }
    const arrival = decideArrival(asked.value.depart_in_min, checked.value.ride.ride_min);
    if (!arrival.ok) {
      return arrival;
    }
    const neighborRows = await fetchNeighborRows(port, fetched.neighbors);
    return {
      ok: true,
      response: buildTripResponse({
        asked: asked.value,
        ride: checked.value.ride,
        arrive_in_min: arrival.value,
        feeds: fetched.feeds,
        from: checked.value.from,
        to: checked.value.to,
        neighbors: fetched.neighbors,
        neighborRows,
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
