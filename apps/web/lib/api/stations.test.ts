/**
 * `/v1/stations` の組み立て（`lib/api/stations.ts`）。
 *
 * ポートを差し替えられるので、DB 無しで分岐を全部通せる。ここで守りたいのは 4 つ。
 *   * **入力の誤りは常に 400**（設定が足りない環境でも 503 に化けない）
 *   * **上限を超えたら切り捨てずに 400**（穴の開いた地図を黙って返さない）
 *   * 値には観測時刻が付き、**不在のポートは null**
 *   * bbox は外側へ丸めた実効値を返す
 */
import { describe, expect, it, vi } from "vitest";
import { STATIONS_MAX_RESULTS, stationsResponseSchema } from "@bikechance/shared";
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
  },
  {
    system_id: "docomo-cycle",
    display_name: "ドコモ・バイクシェア",
    expected_cadence_s: 81,
    poll_interval_s: 60,
    capacity_is_dynamic: true,
    // 303 秒（stale_after_s）を超えている
    last_observed_at: "2026-09-07T23:50:00.000Z",
  },
];

const station = (overrides: Partial<StationRow> = {}): StationRow => ({
  system_id: "hellocycling",
  station_id: "s-1",
  name: "テストポート",
  lat: 35.68,
  lon: 139.77,
  capacity: 10,
  bikes: 3,
  docks: 7,
  is_installed: true,
  is_renting: true,
  is_returning: true,
  is_present: true,
  last_changed_at: "2026-09-07T23:55:00.000Z",
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
  const build = (rows: readonly StationRow[]) =>
    buildStationsResponse({
      bbox: { west: 139.76, south: 35.67, east: 139.78, north: 35.69 },
      feeds: FEEDS,
      rows,
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
      now: new Date(),
    });
    expect(response.generated_at).toBe(NOW.toISOString());
    vi.useRealTimers();
  });
});
