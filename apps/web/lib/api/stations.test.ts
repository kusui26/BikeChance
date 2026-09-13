/**
 * `/v1/stations` の組み立て（`lib/api/stations.ts`）。
 *
 * ポートを差し替えられるので、DB 無しで分岐を全部通せる。ここで守りたいのは 4 つ。
 *   * **入力の誤りは常に 400**（設定が足りない環境でも 503 に化けない）
 *   * **上限を超えたら切り捨てずに 400**（穴の開いた地図を黙って返さない）
 *   * 値には観測時刻が付き、**不在のポートは null**
 *   * bbox は外側へ丸めた実効値を返す
 *   * **予測は指定があるときだけ返し、古ければ返さない**（W4 の PR A）
 */
import { describe, expect, it, vi } from "vitest";
import {
  FORECAST_STALE_AFTER_S,
  HORIZONS_MIN,
  STATIONS_MAX_RESULTS,
  stationsResponseSchema,
} from "@bikechance/shared";
import { buildStationsResponse, queryStations } from "./stations";
import type { FeedRow, ReadPort, StationRow } from "./read-port";

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
    // 303 秒（stale_after_s）を超えている
    last_observed_at: "2026-09-07T23:50:00.000Z",
    forecast_model_version: "b1-2026-09-08",
    forecast_generated_at: "2026-09-07T23:50:30.000Z",
  },
];

const station = (overrides: Partial<StationRow> = {}): StationRow => ({
  system_id: "hellocycling",
  station_id: "s-1",
  name: "テストポート",
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
  // 予測は 5 分刻みで下がっていく表。補間の結果を暗算で確かめられる
  forecast_horizons_min: [...HORIZONS_MIN],
  forecast_p_bike_x1000: [900, 880, 860, 840, 800, 750, 700, 600, 500, 300],
  forecast_p_dock_x1000: [100, 120, 140, 160, 200, 250, 300, 400, 500, 700],
  forecast_confidence: 3,
  forecast_base_observed_at: "2026-09-07T23:58:00.000Z",
  forecast_model_version: "b1-2026-09-08",
  // 水平の起点。NOW の 90 秒前＝推論周期の途中という現実的な年齢
  forecast_generated_at: "2026-09-07T23:58:30.000Z",
  ...overrides,
});

type PortOptions = {
  readonly rows?: readonly StationRow[];
  readonly total?: number;
  readonly feeds?: readonly FeedRow[];
  readonly throwOn?: "feeds" | "stations";
};

const fakePort = (options: PortOptions = {}) => {
  const rows = options.rows ?? [station()];
  const calls: { bbox: unknown; system_id: string | null; limit: number }[] = [];
  const port: ReadPort = {
    listFeeds: async () => {
      if (options.throwOn === "feeds") throw new Error("db down");
      return options.feeds ?? FEEDS;
    },
    listStationsInBbox: async (params) => {
      if (options.throwOn === "stations") throw new Error("db down");
      calls.push({ bbox: params.bbox, system_id: params.system_id, limit: params.limit });
      return { rows, total: options.total ?? rows.length };
    },
    // `/v1/stations` は使わない。`ReadPort` を満たすためだけに置く
    listStationsByIds: async () => [],
    listNeighbors: async () => [],
    findStation: async () => null,
    listRecentHours: async () => [],
  };
  return { port, calls };
};

const run = (query: string, options: PortOptions = {}) => {
  const { port, calls } = fakePort(options);
  return queryStations({
    makePort: () => port,
    search: new URLSearchParams(query),
    now: NOW,
  }).then((outcome) => ({ outcome, calls }));
};

const BBOX = "bbox=139.76,35.67,139.78,35.69";

const failureOf = async (query: string, options: PortOptions = {}): Promise<string> => {
  const { outcome } = await run(query, options);
  return outcome.ok ? "ok" : outcome.failure.code;
};

describe("入力の検証", () => {
  it("bbox が無ければ 400（DB には触らない）", async () => {
    const { outcome, calls } = await run("");
    expect(outcome.ok).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it("bbox の誤りは種類ごとに違う code を返す", async () => {
    expect(await failureOf("")).toBe("bbox_missing");
    expect(await failureOf("bbox=1,2,3")).toBe("bbox_malformed");
    expect(await failureOf("bbox=139.76,-91,139.78,35.69")).toBe("bbox_out_of_range");
    expect(await failureOf("bbox=139.78,35.67,139.76,35.69")).toBe("bbox_inverted");
    expect(await failureOf("bbox=139.0,35.0,140.0,36.0")).toBe("bbox_too_large");
  });

  it("入力の誤りは 400（設定が壊れていても 503 にしない）", async () => {
    const outcome = await queryStations({
      makePort: () => {
        throw new Error("環境変数が無い");
      },
      search: new URLSearchParams(""),
      now: NOW,
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) expect(outcome.failure.status).toBe(400);
  });

  it("未知の system は 400", async () => {
    expect(await failureOf(`${BBOX}&system=nope`)).toBe("unknown_system");
  });

  it("system を渡すと絞り込みに渡る", async () => {
    const { calls } = await run(`${BBOX}&system=docomo-cycle`);
    expect(calls[0]?.system_id).toBe("docomo-cycle");
  });

  it("system 未指定は全システム", async () => {
    const { calls } = await run(BBOX);
    expect(calls[0]?.system_id).toBeNull();
  });
});

describe("bbox の丸め", () => {
  it("外側へ丸めた矩形で問い合わせ、その矩形を返す", async () => {
    const { outcome, calls } = await run("bbox=139.7654,35.6789,139.7712,35.6821");
    expect(calls[0]?.bbox).toEqual({ west: 139.76, south: 35.67, east: 139.78, north: 35.69 });
    if (outcome.ok) {
      expect(outcome.response.bbox).toEqual({
        west: 139.76,
        south: 35.67,
        east: 139.78,
        north: 35.69,
      });
    }
  });
});

describe("件数の上限", () => {
  it("上限を超えたら切り捨てずに 400", async () => {
    const { outcome } = await run(BBOX, { total: STATIONS_MAX_RESULTS + 1 });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.code).toBe("too_many_stations");
      // どれだけ狭めればよいかが分かるよう、該当件数を返す
      expect(outcome.failure.detail).toContain(String(STATIONS_MAX_RESULTS + 1));
    }
  });

  it("上限ちょうどは通る", async () => {
    const { outcome } = await run(BBOX, { total: STATIONS_MAX_RESULTS });
    expect(outcome.ok).toBe(true);
  });

  it("上限をポートに渡す", async () => {
    const { calls } = await run(BBOX);
    expect(calls[0]?.limit).toBe(STATIONS_MAX_RESULTS);
  });
});

describe("DB の不調", () => {
  it("ポートが落ちたら 503", async () => {
    const { outcome } = await run(BBOX, { throwOn: "stations" });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.status).toBe(503);
      expect(outcome.failure.code).toBe("upstream_unavailable");
    }
  });

  it("鮮度が取れないだけでも 503（部分的な応答を返さない）", async () => {
    const { outcome } = await run(BBOX, { throwOn: "feeds" });
    expect(outcome.ok).toBe(false);
  });

  it("理由を応答に載せない", async () => {
    const { outcome } = await run(BBOX, { throwOn: "stations" });
    if (!outcome.ok) expect(outcome.failure.detail).not.toContain("db down");
  });
});

describe("buildStationsResponse", () => {
  const build = (rows: readonly StationRow[], in_min: number | null = null) =>
    buildStationsResponse({
      bbox: { west: 139.76, south: 35.67, east: 139.78, north: 35.69 },
      feeds: FEEDS,
      rows,
      in_min,
      now: NOW,
    });

  it("スキーマに適合する", () => {
    expect(() => stationsResponseSchema.parse(build([station()]))).not.toThrow();
  });

  it("観測時刻はフィードの last_updated（値が変わった時刻ではない）", () => {
    const response = build([station({ last_changed_at: "2026-09-01T00:00:00.000Z" })]);
    expect(response.stations[0]?.observed_at).toBe("2026-09-07T23:58:00.000Z");
    expect(response.stations[0]?.last_changed_at).toBe("2026-09-01T00:00:00.000Z");
  });

  it("最新のフィードに現れなかったポートは観測時刻を null にする", () => {
    const response = build([station({ is_present: false })]);
    expect(response.stations[0]?.observed_at).toBeNull();
    expect(response.stations[0]?.is_present).toBe(false);
  });

  it("システムごとに違う観測時刻を割り当てる", () => {
    const response = build([station(), station({ system_id: "docomo-cycle", station_id: "d-1" })]);
    expect(response.stations[0]?.observed_at).toBe("2026-09-07T23:58:00.000Z");
    expect(response.stations[1]?.observed_at).toBe("2026-09-07T23:50:00.000Z");
  });

  it("未観測の値は null のまま（0 や false にしない）", () => {
    const response = build([
      station({
        bikes: null,
        docks: null,
        is_installed: null,
        is_renting: null,
        is_returning: null,
      }),
    ]);
    const first = response.stations[0];
    expect(first?.bikes).toBeNull();
    expect(first?.is_renting).toBeNull();
  });

  it("属性の無いポートも落とさない", () => {
    const response = build([station({ name: null, capacity: null })]);
    expect(response.count).toBe(1);
    expect(response.stations[0]?.name).toBeNull();
  });

  it("フィードごとに古さを判定する", () => {
    const response = build([station()]);
    const hello = response.feeds.find((feed) => feed.system_id === "hellocycling");
    const docomo = response.feeds.find((feed) => feed.system_id === "docomo-cycle");
    expect(hello?.stale).toBe(false);
    expect(hello?.stale_after_s).toBe(960);
    expect(docomo?.stale).toBe(true);
    expect(docomo?.stale_after_s).toBe(303);
  });

  it("どれか 1 つでも古ければ全体を stale にする", () => {
    expect(build([station()]).stale).toBe(true);
  });

  it("ドコモの capacity が動的であることを伝える", () => {
    const docomo = build([station()]).feeds.find((feed) => feed.system_id === "docomo-cycle");
    expect(docomo?.capacity_is_dynamic).toBe(true);
  });

  it("クレジットを応答に含める（表示を応答だけで完結できる）", () => {
    const response = build([station()]);
    expect(response.attribution).toHaveLength(2);
    expect(response.attribution[0]?.license_url).toContain("creativecommons.org");
  });

  it("count は返した件数と一致する", () => {
    const rows = [station(), station({ station_id: "s-2" }), station({ station_id: "s-3" })];
    expect(build(rows).count).toBe(3);
  });

  it("空の結果でも 200 の形になる", () => {
    const response = build([]);
    expect(response.count).toBe(0);
    expect(response.stations).toEqual([]);
  });
});

describe("時刻の扱い", () => {
  it("generated_at は呼び出し時刻", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const response = buildStationsResponse({
      bbox: { west: 0, south: 0, east: 1, north: 1 },
      feeds: FEEDS,
      rows: [],
      in_min: null,
      now: new Date(),
    });
    expect(response.generated_at).toBe(NOW.toISOString());
    vi.useRealTimers();
  });
});

describe("予測（W4 の PR A）", () => {
  const build = (rows: readonly StationRow[], in_min: number | null) =>
    buildStationsResponse({
      bbox: { west: 139.76, south: 35.67, east: 139.78, north: 35.69 },
      feeds: FEEDS,
      rows,
      in_min,
      now: NOW,
    });

  const forecastOf = (overrides: Partial<StationRow>, in_min: number | null = 30) =>
    build([station(overrides)], in_min).stations[0]?.forecast ?? null;

  /** 起点の効果を分けて見るための、年齢 0 の行。 */
  const justGenerated = { forecast_generated_at: NOW.toISOString() };

  it("**指定が無ければ返さない**（いままでの呼び出しの形を変えない）", () => {
    const response = build([station()], null);
    expect(response.forecast_in_min).toBeNull();
    expect(response.stations[0]?.forecast).toBeNull();
  });

  it("指定があれば水平ちょうどの値をそのまま返す（年齢 0 のとき）", () => {
    const forecast = forecastOf(justGenerated, 30);
    expect(forecast?.p_bike).toBe(0.8);
    expect(forecast?.p_dock).toBe(0.2);
  });

  it("水平の間は補間する", () => {
    // 20 分（840）と 30 分（800）の中点 → 820
    expect(forecastOf(justGenerated, 25)?.p_bike).toBe(0.82);
  });

  it("**丸めた後の分を応答に書く**（何分の予測を見ているかが応答から読める）", async () => {
    const { outcome } = await run(`${BBOX}&in_min=37`);
    expect(outcome.ok).toBe(true);
    if (outcome.ok) {
      expect(outcome.response.forecast_in_min).toBe(35);
      // 35 分 ＋ 行の年齢 1.5 分 = 36.5 分。30 分（800）と 45 分（750）の 0.433 →
      // 778.33 → 1/1000 に丸めて 778
      expect(outcome.response.stations[0]?.forecast?.p_bike).toBe(0.778);
    }
  });

  it("confidence と model_version はそのまま渡す", () => {
    const forecast = forecastOf({ forecast_confidence: 1 });
    expect(forecast?.confidence).toBe(1);
    expect(forecast?.model_version).toBe("b1-2026-09-08");
  });

  it("base_observed_at を返す（利用者が自分でも鮮度を測れる）", () => {
    expect(forecastOf({})?.base_observed_at).toBe("2026-09-07T23:58:00.000Z");
  });

  it("**予測の無いポートも行は返す**（台数は出せる）", () => {
    const response = build([station({ forecast_base_observed_at: null })], 30);
    expect(response.count).toBe(1);
    expect(response.stations[0]?.bikes).toBe(3);
    expect(response.stations[0]?.forecast).toBeNull();
  });

  it("**古い予測は返さない**（11 時間前の確率を「現在の予測」として出さない）", () => {
    const old = new Date(NOW.getTime() - (FORECAST_STALE_AFTER_S + 60) * 1000).toISOString();
    expect(forecastOf({ forecast_base_observed_at: old })).toBeNull();
  });

  it("閾値のちょうどはまだ返す", () => {
    const edge = new Date(NOW.getTime() - FORECAST_STALE_AFTER_S * 1000).toISOString();
    expect(forecastOf({ forecast_base_observed_at: edge })).not.toBeNull();
  });

  it("配列の長さがそろわなければ返さない（欠けた表から数を作らない）", () => {
    expect(forecastOf({ forecast_p_bike_x1000: [900, 880] })).toBeNull();
  });

  it("片方だけでは返さない（借りると返すはどちらも表示に要る）", () => {
    expect(forecastOf({ forecast_p_dock_x1000: null })).toBeNull();
  });

  it("confidence や model_version が欠けていれば返さない", () => {
    expect(forecastOf({ forecast_confidence: null })).toBeNull();
    expect(forecastOf({ forecast_model_version: null })).toBeNull();
  });

  it("予測が無いことを理由として返さない（内部の都合を契約に漏らさない）", () => {
    const response = build([station({ forecast_horizons_min: null })], 30);
    expect(JSON.stringify(response)).not.toContain("reason");
  });

  it("スキーマに適合する（予測あり・なしのどちらも）", () => {
    const rows = [station(), station({ station_id: "s-2", forecast_base_observed_at: null })];
    expect(() => stationsResponseSchema.parse(build(rows, 30))).not.toThrow();
  });
});

describe("到着の指定の検証", () => {
  it("at と in_min の同時指定は 400（DB には触らない）", async () => {
    const { outcome, calls } = await run(`${BBOX}&at=2026-09-08T00:30:00Z&in_min=30`);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) expect(outcome.failure.code).toBe("arrival_conflict");
    expect(calls).toHaveLength(0);
  });

  it("範囲の外は 400（末尾の値を張り付けて返さない）", async () => {
    expect(await failureOf(`${BBOX}&in_min=999`)).toBe("arrival_out_of_range");
    expect(await failureOf(`${BBOX}&in_min=1`)).toBe("arrival_out_of_range");
  });

  it("読めない指定は 400", async () => {
    expect(await failureOf(`${BBOX}&in_min=abc`)).toBe("arrival_malformed");
    // タイムゾーンなし
    expect(await failureOf(`${BBOX}&at=2026-09-08T00:30:00`)).toBe("arrival_malformed");
  });

  it("誤った指定でも DB には触らない（無駄な問い合わせを出さない）", async () => {
    const { calls } = await run(`${BBOX}&in_min=999`);
    expect(calls).toHaveLength(0);
  });

  it("at でも予測が付く", async () => {
    const { outcome } = await run(`${BBOX}&at=2026-09-08T00:30:00Z`);
    expect(outcome.ok).toBe(true);
    if (outcome.ok) {
      expect(outcome.response.forecast_in_min).toBe(30);
      // 30 分 ＋ 行の年齢 1.5 分 = 31.5 分の位置（水平ちょうどの 0.8 ではない）
      expect(outcome.response.stations[0]?.forecast?.p_bike).toBe(0.795);
    }
  });

  it("指定が無ければ予測は付かない（既定の応答）", async () => {
    const { outcome } = await run(BBOX);
    expect(outcome.ok).toBe(true);
    if (outcome.ok) {
      expect(outcome.response.forecast_in_min).toBeNull();
      expect(outcome.response.stations[0]?.forecast).toBeNull();
    }
  });
});

describe("水平の起点（W4 プラン §12 の 114）", () => {
  const build = (rows: readonly StationRow[], in_min: number | null) =>
    buildStationsResponse({
      bbox: { west: 139.76, south: 35.67, east: 139.78, north: 35.69 },
      feeds: FEEDS,
      rows,
      in_min,
      now: NOW,
    });

  const aged = (age_s: number) =>
    build(
      [station({ forecast_generated_at: new Date(NOW.getTime() - age_s * 1000).toISOString() })],
      30,
    ).stations[0]?.forecast ?? null;

  it("**行の年齢を足した位置で読む**（起点は generated_at）", () => {
    // 30 分 ＋ 1.5 分 = 31.5 分 → 30 分（800）と 45 分（750）の 0.1 → 795
    expect(aged(90)?.p_bike).toBe(0.795);
    expect(aged(90)?.p_dock).toBe(0.205);
  });

  it("年齢を無視すると別の時刻の確率になる（回帰の見張り）", () => {
    // 起点を足さなければ 0.8（＝水平 30 分ちょうど）。足せば 0.795
    expect(aged(90)?.p_bike).not.toBe(0.8);
  });

  it("年齢が大きいほど先を読む", () => {
    expect(aged(0)?.p_bike).toBe(0.8);
    expect(aged(300)?.p_bike).toBe(0.783);
    // 単調に下がる表なので、古い行ほど小さい値になる
    const values = [aged(0), aged(90), aged(300)].map((one) => one?.p_bike ?? 0);
    expect(values[0]).toBeGreaterThan(values[1] ?? 0);
    expect(values[1]).toBeGreaterThan(values[2] ?? 0);
  });

  it("未来の時刻は 0 として扱う（頼まれた到着より手前を読まない）", () => {
    expect(aged(-600)?.p_bike).toBe(0.8);
  });

  it("起点が無ければ予測を返さない（位置を決められない）", () => {
    const forecast =
      build([station({ forecast_generated_at: null })], 30).stations[0]?.forecast ?? null;
    expect(forecast).toBeNull();
  });

  it("返す確率が指すのは generated_at ＋ forecast_in_min の時刻", () => {
    const response = build([station()], 30);
    // 応答の generated_at は呼び出し時刻。利用者はこの 2 つで到着の時刻を復元できる
    expect(response.generated_at).toBe(NOW.toISOString());
    expect(response.forecast_in_min).toBe(30);
  });
});
