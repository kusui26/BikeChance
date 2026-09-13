/**
 * `/v1/trip-check` の組み立て（`lib/api/trip-check.ts`）。
 *
 * ポートを差し替えられるので、DB 無しで分岐を全部通せる。**この API の固有の危うさは
 * 「時刻が 2 つあること」**で、取り違えても例外は出ず確率だけが静かに変わる。
 * ここで守りたいのは 6 つ。
 *   * **出発と到着で違う時刻の確率を読む**（同じ時刻を 2 回読んでいないこと）
 *   * **`trip` は両端がそろったときだけ**（契約 10）。掛け算の片側が欠けた値を作らない
 *   * **到着が水平を超えたら断る**（超えると末尾に張り付いて別の時刻の確率になる）
 *   * **系統をまたぐ ID は、そう言って断る**（W4-21。書けなくすることと診断は別）
 *   * **代替候補は確率の高い順**で、予測の無いポートは入れない（W4-25）
 *   * **入力の誤りは常に 400**（設定が足りない環境でも 503 に化けない）
 */
import { describe, expect, it } from "vitest";
import {
  FORECAST_STALE_AFTER_S,
  HORIZONS_MIN,
  TRIP_ALTERNATIVES_MAX,
  TRIP_INDEPENDENCE_NOTICE,
  tripCheckResponseSchema,
} from "@bikechance/shared";
import { queryTrip } from "./trip-check";
import type { FeedRow, NeighborRow, ReadPort, StationRow } from "./read-port";

const NOW = new Date("2026-09-08T00:00:00.000Z");

const FEEDS: readonly FeedRow[] = [
  {
    system_id: "hellocycling",
    display_name: "HELLO CYCLING",
    expected_cadence_s: 300,
    poll_interval_s: 60,
    capacity_is_dynamic: false,
    last_observed_at: "2026-09-07T23:58:00.000Z",
    forecast_model_version: "b1-2026-09-08",
    forecast_generated_at: "2026-09-07T23:58:30.000Z",
  },
  {
    system_id: "docomo-cycle",
    display_name: "ドコモ・バイクシェア",
    expected_cadence_s: 81,
    poll_interval_s: 60,
    capacity_is_dynamic: true,
    last_observed_at: "2026-09-07T23:50:00.000Z",
    forecast_model_version: "b1-2026-09-08",
    forecast_generated_at: "2026-09-07T23:50:30.000Z",
  },
];

/**
 * 水平ごとに**別の値**を持つ表にする。**補間した時刻がずれれば値が変わる**ので、
 * 「出発と到着で違う時刻を読んでいるか」をテストが見抜ける。
 *
 * `forecast_generated_at` は NOW ちょうどにしてある（行の年齢 0）。年齢の扱いは
 * `station-row.ts` と `forecast.ts` の持ち場で、ここでは**時刻の取り違え**だけを見たい。
 */
const station = (overrides: Partial<StationRow> = {}): StationRow => ({
  system_id: "hellocycling",
  station_id: "from-1",
  name: "出発ポート",
  lat: 35.68,
  lon: 139.77,
  capacity: 10,
  capacity_est: 12,
  capacity_days: 7,
  bikes: 3,
  docks: 7,
  is_installed: true,
  is_renting: true,
  is_returning: true,
  is_present: true,
  last_changed_at: "2026-09-07T23:55:00.000Z",
  forecast_horizons_min: [...HORIZONS_MIN],
  // 借りられる確率は先へ行くほど下がる / 返せる確率は上がる
  forecast_p_bike_x1000: [900, 880, 860, 840, 800, 750, 700, 600, 500, 300],
  forecast_p_dock_x1000: [100, 120, 140, 160, 200, 250, 300, 400, 500, 700],
  forecast_confidence: 3,
  forecast_base_observed_at: "2026-09-07T23:58:00.000Z",
  forecast_model_version: "b1-2026-09-08",
  forecast_generated_at: "2026-09-08T00:00:00.000Z",
  ...overrides,
});

type PortOptions = {
  readonly rows?: readonly StationRow[];
  readonly neighbors?: readonly NeighborRow[];
  readonly throwOn?: "feeds" | "stations" | "neighbors";
};

const fakePort = (options: PortOptions = {}): ReadPort => {
  const rows = options.rows ?? [station(), station({ station_id: "to-1", name: "到着ポート" })];
  return {
    listFeeds: async () => {
      if (options.throwOn === "feeds") throw new Error("db down");
      return FEEDS;
    },
    listStationsInBbox: async () => ({ rows: [], total: 0 }),
    listStationsByIds: async ({ station_ids }) => {
      if (options.throwOn === "stations") throw new Error("db down");
      return rows.filter((row) => station_ids.includes(row.station_id));
    },
    listNeighbors: async () => {
      if (options.throwOn === "neighbors") throw new Error("db down");
      return options.neighbors ?? [];
    },
    // `/v1/trip-check` は使わない。`ReadPort` を満たすためだけに置く
    findStation: async () => null,
    listRecentHours: async () => [],
  };
};

const ask = async (query: Record<string, string>, options: PortOptions = {}) =>
  queryTrip({
    makePort: () => fakePort(options),
    search: new URLSearchParams(query),
    now: NOW,
  });

const BASIC = { system: "hellocycling", from: "from-1", to: "to-1", depart_in_min: "10" };

/** 失敗なら code、成功なら null。分岐を 1 行で見るため。 */
const codeOf = async (query: Record<string, string>, options: PortOptions = {}) => {
  const outcome = await ask(query, options);
  return outcome.ok ? null : outcome.failure.code;
};

// ── 時刻が 2 つあること ──────────────────────────────────────
describe("出発と到着で違う時刻を読む", () => {
  it("**出発は depart、到着は arrive の時刻**（同じ時刻を 2 回読んでいない）", async () => {
    // 出発 10 分後・乗車 20 分 → 到着 30 分後
    const outcome = await ask({ ...BASIC, ride_min: "20" });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.depart_in_min).toBe(10);
    expect(outcome.response.arrive_in_min).toBe(30);
    // 表の 10 分と 30 分の値（行の年齢 0 なので水平そのもの）
    expect(outcome.response.from.forecast?.p_bike).toBe(0.88);
    expect(outcome.response.to.forecast?.p_dock).toBe(0.2);
  });

  it("乗車 0 分なら両端が同じ時刻の値になる（境目の確認）", async () => {
    const outcome = await ask({ ...BASIC, ride_min: "0" });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.arrive_in_min).toBe(10);
    expect(outcome.response.to.forecast?.p_dock).toBe(0.12);
  });

  it("**出発は 5 分に丸めてから使う**（`/v1/stations` と同じ規則）", async () => {
    const outcome = await ask({ ...BASIC, depart_in_min: "12", ride_min: "3" });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.depart_in_min).toBe(10);
    expect(outcome.response.arrive_in_min).toBe(13);
  });
});

// ── ride_min ────────────────────────────────────────────────
describe("乗車時間", () => {
  it("渡されたらそのまま使い、概算していないことを示す", async () => {
    const outcome = await ask({ ...BASIC, ride_min: "7" });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.ride_min).toBe(7);
    expect(outcome.response.ride_min_estimated).toBe(false);
  });

  it("省略されたら座標から概算し、**概算したことを示す**", async () => {
    const outcome = await ask(BASIC, {
      rows: [
        station({ lat: 35.68, lon: 139.77 }),
        station({ station_id: "to-1", lat: 35.7, lon: 139.79 }),
      ],
    });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.ride_min_estimated).toBe(true);
    expect(outcome.response.ride_min).toBeGreaterThan(0);
  });
});

describe("座標の無いポート", () => {
  // ビューは座標の無いポートを持つ（実測 10 件。うち 1 件は予測も持っていた）。
  // bbox では外れるが **ID で引くと出てくる**——`/v1/trip-check` 固有の経路である
  it("**端点にはできない**（地図に出せない先へは行けない）", async () => {
    expect(
      await codeOf(
        { ...BASIC, ride_min: "9" },
        { rows: [station({ lat: null, lon: null }), station({ station_id: "to-1" })] },
      ),
    ).toBe("station_location_missing");
  });

  it("`ride_min` を渡しても断る（乗車時間の問題ではない）", async () => {
    expect(
      await codeOf(BASIC, {
        rows: [station(), station({ station_id: "to-1", lat: null, lon: null })],
      }),
    ).toBe("station_location_missing");
  });

  it("どちらの端点かを本文で言う", async () => {
    const outcome = await ask(BASIC, {
      rows: [station(), station({ station_id: "to-1", lat: null, lon: null })],
    });
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.detail).toContain("to");
  });

  it("**代替候補にも入れない**（歩いて向かう先なので同じ理由）", async () => {
    const outcome = await ask(BASIC, {
      rows: [
        station(),
        station({ station_id: "to-1" }),
        station({ station_id: "blind", lat: null, lon: null }),
      ],
      neighbors: [{ station_id: "from-1", nb_station_id: "blind", distance_m: 50 }],
    });
    expect(outcome.ok && outcome.response.alternatives.from).toEqual([]);
  });
});

// ── 到着の範囲（W4-22） ─────────────────────────────────────
describe("到着が水平を超えたら断る", () => {
  const MAX = Math.max(...HORIZONS_MIN);

  it("端ちょうどは通る", async () => {
    const outcome = await ask({ ...BASIC, depart_in_min: "5", ride_min: String(MAX - 5) });
    expect(outcome.ok).toBe(true);
  });

  it("**1 分超えたら 400**（末尾に張り付いた値を返さない）", async () => {
    expect(await codeOf({ ...BASIC, depart_in_min: "5", ride_min: String(MAX - 4) })).toBe(
      "arrival_out_of_range",
    );
  });

  it("出発そのものが範囲外でも 400", async () => {
    expect(await codeOf({ ...BASIC, depart_in_min: "500" })).toBe("arrival_out_of_range");
  });
});

// ── trip（契約 10） ─────────────────────────────────────────
describe("trip は両端がそろったときだけ", () => {
  it("そろえば掛け算と注記が入る", async () => {
    const outcome = await ask({ ...BASIC, ride_min: "20" });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.trip?.p_trip).toBe(0.176); // 0.88 × 0.20
    expect(outcome.response.trip?.notice).toBe(TRIP_INDEPENDENCE_NOTICE);
  });

  it("**出発の予測が無ければ null**（片側で代用しない）", async () => {
    const outcome = await ask(BASIC, {
      rows: [station({ forecast_p_bike_x1000: null }), station({ station_id: "to-1" })],
    });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.from.forecast).toBeNull();
    expect(outcome.response.trip).toBeNull();
    // 到着側は出せているので、そちらは残る
    expect(outcome.response.to.forecast).not.toBeNull();
  });

  it("**到着の予測が無くても null**", async () => {
    const outcome = await ask(BASIC, {
      rows: [station(), station({ station_id: "to-1", forecast_confidence: null })],
    });
    expect(outcome.ok && outcome.response.trip).toBeNull();
  });

  it("**基づく観測が古ければ予測ごと出さない**（鮮度は base_observed_at で測る）", async () => {
    const stale = new Date(NOW.getTime() - (FORECAST_STALE_AFTER_S + 60) * 1000).toISOString();
    const outcome = await ask(BASIC, {
      rows: [station({ forecast_base_observed_at: stale }), station({ station_id: "to-1" })],
    });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.from.forecast).toBeNull();
    expect(outcome.response.trip).toBeNull();
  });

  it("confidence は**小さいほう**（鎖は弱い環の強さ）", async () => {
    const outcome = await ask(BASIC, {
      rows: [
        station({ forecast_confidence: 3 }),
        station({ station_id: "to-1", forecast_confidence: 1 }),
      ],
    });
    expect(outcome.ok && outcome.response.trip?.confidence).toBe(1);
  });
});

// ── 入力の検証（W4-21） ─────────────────────────────────────
describe("入力の誤りは 400", () => {
  it("system が無い / 知らない", async () => {
    expect(await codeOf({ from: "from-1", to: "to-1", depart_in_min: "10" })).toBe(
      "unknown_system",
    );
    expect(await codeOf({ ...BASIC, system: "nope" })).toBe("unknown_system");
  });

  it("from / to が無い", async () => {
    expect(await codeOf({ system: "hellocycling", depart_in_min: "10" })).toBe("station_missing");
    expect(await codeOf({ ...BASIC, to: "  " })).toBe("station_missing");
  });

  it("from と to が同じ", async () => {
    expect(await codeOf({ ...BASIC, to: "from-1" })).toBe("same_station");
  });

  it("出発の指定が無い", async () => {
    expect(await codeOf({ system: "hellocycling", from: "from-1", to: "to-1" })).toBe(
      "depart_missing",
    );
  });

  it("出発を 2 通りで指定した", async () => {
    expect(await codeOf({ ...BASIC, depart_at: "2026-09-08T00:20:00Z" })).toBe("arrival_conflict");
  });

  it("**本文は depart_at / depart_in_min と言う**（存在しない引数名を案内しない）", async () => {
    const outcome = await ask({ ...BASIC, depart_at: "2026-09-08T00:20:00Z" });
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.detail).toContain("depart_at");
    expect(outcome.failure.detail).toContain("depart_in_min");
  });

  it("ride_min が整数でない", async () => {
    expect(await codeOf({ ...BASIC, ride_min: "8.5" })).toBe("ride_malformed");
    expect(await codeOf({ ...BASIC, ride_min: "-1" })).toBe("ride_out_of_range");
  });
});

describe("知らないポート", () => {
  it("どこにも無ければ、その系統に無いと言う", async () => {
    const outcome = await ask({ ...BASIC, to: "nope" });
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.code).toBe("unknown_station");
    expect(outcome.failure.detail).toContain("hellocycling にありません");
  });

  it("**別の系統に在るなら、そう言う**（W4-21。書けなくすることと診断は別）", async () => {
    const outcome = await ask(BASIC, {
      rows: [station(), station({ system_id: "docomo-cycle", station_id: "to-1" })],
    });
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.code).toBe("unknown_station");
    expect(outcome.failure.detail).toContain("docomo-cycle のポートです");
  });

  it("from の誤りを先に言う（2 つとも誤っていても 1 つずつ直せる）", async () => {
    const outcome = await ask({ ...BASIC, from: "nope-a", to: "nope-b" }, { rows: [] });
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.detail).toContain("from");
  });
});

// ── 代替候補（W4-25） ──────────────────────────────────────
describe("代替候補", () => {
  const neighbor = (id: string, distance_m: number, station_id = "from-1"): NeighborRow => ({
    station_id,
    nb_station_id: id,
    distance_m,
  });

  /** 確率を 1 つだけ変えた近傍を作る。`p_bike[10 分]` が `bike` になる。 */
  const alt = (id: string, bike: number, dock: number): StationRow =>
    station({
      station_id: id,
      forecast_p_bike_x1000: HORIZONS_MIN.map(() => bike),
      forecast_p_dock_x1000: HORIZONS_MIN.map(() => dock),
    });

  it("**確率の高い順**に並ぶ（距離順ではない）", async () => {
    const outcome = await ask(BASIC, {
      rows: [
        station(),
        station({ station_id: "to-1" }),
        alt("near-low", 200, 100),
        alt("far-high", 900, 100),
      ],
      neighbors: [neighbor("near-low", 50), neighbor("far-high", 390)],
    });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.alternatives.from.map((one) => one.station.station_id)).toEqual([
      "far-high",
      "near-low",
    ]);
  });

  it("**到着側は返せる確率で並ぶ**（借りられる確率ではない）", async () => {
    const outcome = await ask(BASIC, {
      rows: [
        station(),
        station({ station_id: "to-1" }),
        alt("bike-rich", 900, 100),
        alt("dock-rich", 100, 900),
      ],
      neighbors: [neighbor("bike-rich", 50, "to-1"), neighbor("dock-rich", 100, "to-1")],
    });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.alternatives.to[0]?.station.station_id).toBe("dock-rich");
  });

  it("**衝突した ID で別系統のポートを候補にしない**（W5 プラン §12 の 158）", async () => {
    // `listStationsByIds` は**わざと系統で絞らない**（W4-21）ので、同じ `station_id` を
    // 持つ別系統の行が一緒に返る。**索引を `station_id` だけで作ると後勝ちで上書きされ、
    // 遠くのポートが「50 m 先」として出る**——本番では虎ノ門の行程に厚木（約 40 km）の
    // ポートが出ていた
    const outcome = await ask(BASIC, {
      rows: [
        station(),
        station({ station_id: "to-1" }),
        alt("collide", 900, 100),
        // **同じ ID・別系統・遠くの座標。** 本番の 313 件の衝突と同じ形
        { ...alt("collide", 100, 900), system_id: "docomo-cycle", lat: 35.44, lon: 139.37 },
      ],
      neighbors: [neighbor("collide", 50)],
    });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    const found = outcome.response.alternatives.from;
    expect(found).toHaveLength(1);
    expect(found[0]?.station.system_id).toBe("hellocycling");
    expect(found[0]?.station.lat).toBe(35.68);
  });

  it("**衝突した ID しか無ければ、候補に出さない**（黙って別系統を出さない）", async () => {
    const outcome = await ask(BASIC, {
      rows: [
        station(),
        station({ station_id: "to-1" }),
        { ...alt("only-other", 900, 100), system_id: "docomo-cycle", lat: 35.44, lon: 139.37 },
      ],
      neighbors: [neighbor("only-other", 50)],
    });
    expect(outcome.ok && outcome.response.alternatives.from).toHaveLength(0);
  });

  it(`上限は ${TRIP_ALTERNATIVES_MAX} 件`, async () => {
    const many = Array.from({ length: 9 }, (_, index) => alt(`n${index}`, 100 + index, 100));
    const outcome = await ask(BASIC, {
      rows: [station(), station({ station_id: "to-1" }), ...many],
      neighbors: many.map((row, index) => neighbor(row.station_id, 10 + index)),
    });
    expect(outcome.ok && outcome.response.alternatives.from).toHaveLength(TRIP_ALTERNATIVES_MAX);
  });

  it("**予測を出せないポートは入れない**（確率を答えられない候補は問いに答えていない）", async () => {
    const outcome = await ask(BASIC, {
      rows: [
        station(),
        station({ station_id: "to-1" }),
        station({ station_id: "blind", forecast_horizons_min: null }),
      ],
      neighbors: [neighbor("blind", 50)],
    });
    expect(outcome.ok && outcome.response.alternatives.from).toEqual([]);
  });

  it("距離を添える（画面が歩く距離を出せる）", async () => {
    const outcome = await ask(BASIC, {
      rows: [station(), station({ station_id: "to-1" }), alt("near", 500, 500)],
      neighbors: [neighbor("near", 123)],
    });
    expect(outcome.ok && outcome.response.alternatives.from[0]?.distance_m).toBe(123);
  });

  it("**代替も端点と同じ時刻で評価する**（出発側は depart、到着側は arrive）", async () => {
    const outcome = await ask(
      { ...BASIC, ride_min: "20" },
      {
        rows: [station(), station({ station_id: "to-1" }), station({ station_id: "both" })],
        neighbors: [neighbor("both", 50), neighbor("both", 50, "to-1")],
      },
    );
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    // 同じポートが両側の候補に出る。**読んでいる時刻が違うので値が違う**
    expect(outcome.response.alternatives.from[0]?.station.forecast?.p_bike).toBe(0.88);
    expect(outcome.response.alternatives.to[0]?.station.forecast?.p_bike).toBe(0.8);
  });

  it("近傍が無ければ空（問い合わせも投げない）", async () => {
    const outcome = await ask(BASIC);
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.alternatives).toEqual({ from: [], to: [] });
  });
});

// ── 応答の形 ────────────────────────────────────────────────
describe("応答", () => {
  it("スキーマを満たす", async () => {
    const outcome = await ask({ ...BASIC, ride_min: "20" });
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(() => tripCheckResponseSchema.parse(outcome.response)).not.toThrow();
  });

  it("観測時刻とクレジットを添える（応答だけで表示を完結できる）", async () => {
    const outcome = await ask(BASIC);
    expect(outcome.ok).toBe(true);
    if (!outcome.ok) return;
    expect(outcome.response.from.observed_at).toBe("2026-09-07T23:58:00.000Z");
    expect(outcome.response.attribution.length).toBeGreaterThan(0);
    expect(outcome.response.feeds).toHaveLength(2);
  });

  it("いずれかのフィードが古ければ stale（ドコモが古い前提データ）", async () => {
    const outcome = await ask(BASIC);
    expect(outcome.ok && outcome.response.stale).toBe(true);
  });
});

// ── DB の不調 ───────────────────────────────────────────────
describe("DB に届かない", () => {
  it.each(["feeds", "stations", "neighbors"] as const)("%s が落ちたら 503", async (throwOn) => {
    const outcome = await ask(BASIC, { throwOn });
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.status).toBe(503);
    expect(outcome.failure.code).toBe("upstream_unavailable");
  });

  it("**入力の誤りは DB に触る前に 400**（設定が足りなくても 503 に化けない）", async () => {
    const outcome = await queryTrip({
      makePort: () => {
        throw new Error("環境変数が足りない");
      },
      search: new URLSearchParams({ ...BASIC, system: "nope" }),
      now: NOW,
    });
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.failure.status).toBe(400);
  });
});
