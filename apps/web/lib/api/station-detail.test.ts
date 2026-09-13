/**
 * `/v1/stations/{system}/{station_id}` の組み立て（`lib/api/station-detail.ts`）。
 *
 * ポートを差し替えられるので、DB 無しで分岐を全部通せる。ここで守りたいのは 6 つ。
 *   * **経路の誤りは 400、知らないポートは 404**（設定が足りない環境でも 503 に化けない）
 *   * **曲線は補間しない**（生の 10 点。W5-13）——地図の 1 点と正が同じであること
 *   * **予測が無くても 200**（欄ごと optional。契約 2）
 *   * **鮮度切れの予測は返さない**（`base_observed_at` で測る）
 *   * **直近の実績は 24 時間で切り、観測の無い時間帯は行ごと欠ける**
 *   * **`capacity_est` は `capacity` と別の欄**（W5-14）
 */
import { describe, expect, it } from "vitest";
import {
  FORECAST_STALE_AFTER_S,
  HORIZONS_MIN,
  stationDetailResponseSchema,
} from "@bikechance/shared";
import { RECENT_HOURS, buildStationDetailResponse, queryStationDetail } from "./station-detail";
import type { FeedRow, HourlyRow, ReadPort, StationRow } from "./read-port";

const NOW = new Date("2026-09-08T00:00:00.000Z");

const FEED: FeedRow = {
  system_id: "hellocycling",
  display_name: "HELLO CYCLING",
  expected_cadence_s: 300,
  poll_interval_s: 60,
  capacity_is_dynamic: false,
  last_observed_at: "2026-09-07T23:58:00.000Z",
  forecast_model_version: "b3-2026-09-12",
  forecast_generated_at: "2026-09-07T23:58:30.000Z",
};

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
  forecast_horizons_min: [...HORIZONS_MIN],
  forecast_p_bike_x1000: [900, 880, 860, 840, 800, 750, 700, 600, 500, 300],
  forecast_p_dock_x1000: [100, 120, 140, 160, 200, 250, 300, 400, 500, 700],
  forecast_confidence: 3,
  forecast_base_observed_at: "2026-09-07T23:58:00.000Z",
  forecast_generated_at: "2026-09-07T23:58:30.000Z",
  forecast_model_version: "b3-2026-09-12",
  ...overrides,
});

const hour = (offset_h: number, n = 12): HourlyRow => ({
  station_id: "s-1",
  hour_start: new Date(NOW.getTime() - offset_h * 3_600_000).toISOString(),
  n,
  bikes_mean: 3.5,
  docks_mean: 6.5,
});

type PortOptions = {
  readonly row?: StationRow | null;
  readonly recent?: readonly HourlyRow[];
  readonly feeds?: readonly FeedRow[];
  readonly throwOn?: "feeds" | "station" | "recent";
};

const fakePort = (options: PortOptions = {}) => {
  const asked: { since?: Date; station_id?: string } = {};
  const port: ReadPort = {
    listFeeds: async () => {
      if (options.throwOn === "feeds") throw new Error("db down");
      return options.feeds ?? [FEED];
    },
    findStation: async ({ station_id }) => {
      if (options.throwOn === "station") throw new Error("db down");
      asked.station_id = station_id;
      return options.row === undefined ? station() : options.row;
    },
    listRecentHours: async ({ since }) => {
      if (options.throwOn === "recent") throw new Error("db down");
      asked.since = since;
      return options.recent ?? [];
    },
    // この経路では使わない。`ReadPort` を満たすためだけに置く
    listStationsInBbox: async () => ({ rows: [], total: 0 }),
    listStationsByIds: async () => [],
    listNeighbors: async () => [],
  };
  return { port, asked };
};

const ask = (overrides: Partial<Parameters<typeof queryStationDetail>[0]> = {}) => {
  const { port } = fakePort();
  return queryStationDetail({
    makePort: () => port,
    system: "hellocycling",
    station_id: "s-1",
    now: NOW,
    ...overrides,
  });
};

describe("経路の検証", () => {
  it("知らないシステムは DB に触る前に 400", async () => {
    const outcome = await queryStationDetail({
      makePort: () => {
        throw new Error("ここに来てはいけない");
      },
      system: "unknown",
      station_id: "s-1",
      now: NOW,
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.status).toBe(400);
      expect(outcome.failure.code).toBe("unknown_system");
    }
  });

  it("空の station_id は 404（DB に触らない）", async () => {
    const outcome = await queryStationDetail({
      makePort: () => {
        throw new Error("ここに来てはいけない");
      },
      system: "hellocycling",
      station_id: "",
      now: NOW,
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) expect(outcome.failure.status).toBe(404);
  });

  it("知らないポートは 404", async () => {
    const { port } = fakePort({ row: null });
    const outcome = await queryStationDetail({
      makePort: () => port,
      system: "hellocycling",
      station_id: "nope",
      now: NOW,
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.status).toBe(404);
      expect(outcome.failure.code).toBe("station_not_found");
    }
  });

  it("**座標の無いポートも 404**（地図に出ないものの詳細を URL から開けない）", async () => {
    const { port } = fakePort({ row: station({ lat: null, lon: null }) });
    const outcome = await queryStationDetail({
      makePort: () => port,
      system: "hellocycling",
      station_id: "s-1",
      now: NOW,
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) expect(outcome.failure.status).toBe(404);
  });

  it("**そのシステムのフィードが無ければ 404**（停止中のシステム）", async () => {
    const { port } = fakePort({ feeds: [] });
    const outcome = await queryStationDetail({
      makePort: () => port,
      system: "hellocycling",
      station_id: "s-1",
      now: NOW,
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) expect(outcome.failure.status).toBe(404);
  });

  it("DB が落ちていれば 503（理由は載せない）", async () => {
    const { port } = fakePort({ throwOn: "station" });
    const outcome = await queryStationDetail({
      makePort: () => port,
      system: "hellocycling",
      station_id: "s-1",
      now: NOW,
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.status).toBe(503);
      expect(outcome.failure.detail).not.toMatch(/db down/);
    }
  });
});

describe("応答", () => {
  it("スキーマを満たす", async () => {
    const outcome = await ask();
    expect(outcome.ok).toBe(true);
    if (outcome.ok) {
      expect(() => stationDetailResponseSchema.parse(outcome.response)).not.toThrow();
    }
  });

  it("**そのシステムのフィードと表示だけを返す**（地図と違い 1 系統）", async () => {
    const outcome = await ask();
    if (!outcome.ok) throw new Error("ok を期待した");
    expect(outcome.response.feeds).toHaveLength(1);
    expect(outcome.response.feeds[0]?.system_id).toBe("hellocycling");
    expect(outcome.response.attribution.every((one) => one.system_id === "hellocycling")).toBe(
      true,
    );
  });

  it("**`capacity_est` は `capacity` と別の欄**（W5-14）", async () => {
    const outcome = await ask();
    if (!outcome.ok) throw new Error("ok を期待した");
    expect(outcome.response.station.capacity).toBe(10);
    expect(outcome.response.station.capacity_est).toBe(12);
    expect(outcome.response.station.capacity_days).toBe(7);
  });

  it("**地図の `forecast` は持たない**（曲線が正。同じ確率を 2 つの形で配らない）", async () => {
    const outcome = await ask();
    if (!outcome.ok) throw new Error("ok を期待した");
    expect(outcome.response.station.forecast).toBeNull();
  });
});

describe("予測の曲線（W5-13）", () => {
  it("**補間しない。** 生の 10 点がそのまま出る", () => {
    const response = buildStationDetailResponse({
      row: station(),
      feed: FEED,
      recent: [],
      now: NOW,
    });
    expect(response?.forecast_curve?.horizons_min).toEqual([...HORIZONS_MIN]);
    // 1/1000 刻みの整数がそのまま 0〜1 になる（補間の丸めが入らない）
    expect(response?.forecast_curve?.p_bike).toEqual([
      0.9, 0.88, 0.86, 0.84, 0.8, 0.75, 0.7, 0.6, 0.5, 0.3,
    ]);
    expect(response?.forecast_curve?.p_dock[0]).toBe(0.1);
  });

  it("予測が無ければ null（それでも 200）", () => {
    const row = station({
      forecast_horizons_min: null,
      forecast_p_bike_x1000: null,
      forecast_p_dock_x1000: null,
      forecast_confidence: null,
      forecast_base_observed_at: null,
      forecast_model_version: null,
      forecast_generated_at: null,
    });
    const response = buildStationDetailResponse({ row, feed: FEED, recent: [], now: NOW });
    expect(response?.forecast_curve).toBeNull();
    expect(response?.station.station_id).toBe("s-1");
  });

  it("**鮮度切れは返さない**（`base_observed_at` で測る）", () => {
    const stale = new Date(NOW.getTime() - (FORECAST_STALE_AFTER_S + 60) * 1000).toISOString();
    const row = station({ forecast_base_observed_at: stale });
    const response = buildStationDetailResponse({ row, feed: FEED, recent: [], now: NOW });
    expect(response?.forecast_curve).toBeNull();
  });

  it("**長さがそろっていなければ返さない**（欠けた表から数を作らない）", () => {
    const row = station({ forecast_p_bike_x1000: [900, 880] });
    const response = buildStationDetailResponse({ row, feed: FEED, recent: [], now: NOW });
    expect(response?.forecast_curve).toBeNull();
  });
});

describe("直近の実績", () => {
  it("**24 時間で切る**（ビューは 26 時間持っている）", async () => {
    const { port, asked } = fakePort();
    await queryStationDetail({
      makePort: () => port,
      system: "hellocycling",
      station_id: "s-1",
      now: NOW,
    });
    // **`RECENT_HOURS` と比べない。** 同じ式を両辺に置くと、値を変えても落ちない
    // （**壊して確かめたら落ちなかった**。W5 プラン §12 の 154）
    expect(RECENT_HOURS).toBe(24);
    expect(asked.since?.toISOString()).toBe("2026-09-07T00:00:00.000Z");
  });

  it("**観測の無い時間帯は行ごと欠ける**（0 を作らない）", async () => {
    const { port } = fakePort({ recent: [hour(3), hour(1)] });
    const outcome = await queryStationDetail({
      makePort: () => port,
      system: "hellocycling",
      station_id: "s-1",
      now: NOW,
    });
    if (!outcome.ok) throw new Error("ok を期待した");
    expect(outcome.response.recent).toHaveLength(2);
    expect(outcome.response.recent[0]?.n).toBe(12);
  });

  it("24 件までしか入らない（スキーマが上限を持つ）", () => {
    const many = Array.from({ length: 25 }, (_, index) => hour(index + 1));
    expect(() =>
      buildStationDetailResponse({ row: station(), feed: FEED, recent: many, now: NOW }),
    ).toThrow();
  });
});
