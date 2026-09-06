import { describe, expect, it } from "vitest";
import {
  buildIngestArgs,
  buildIngestArrays,
  hasConsistentLengths,
  normalizeStationStatus,
  parseStationStatusFeed,
  toIsoUtc,
  type NormalizedFeed,
} from "../src/index";
import { readFeedFixture } from "./fixtures";

const FETCHED_AT = new Date("2026-09-06T06:52:00.000Z");

const normalizeFixture = (system_id: "hellocycling" | "docomo-cycle"): NormalizedFeed => {
  const result = parseStationStatusFeed(readFeedFixture(system_id, "station_status"));
  if (!result.ok) throw new Error(result.issues.join());
  return normalizeStationStatus(result.feed);
};

describe("toIsoUtc", () => {
  it("POSIX 秒を UTC の ISO 8601 にする", () => {
    expect(toIsoUtc(1_788_677_493)).toBe("2026-09-06T06:51:33.000Z");
  });

  it("実行環境のタイムゾーンに左右されない", () => {
    // ISO 文字列は必ず Z で終わる。ローカル時刻が混ざれば +09:00 になって気づく
    expect(toIsoUtc(1_788_677_493).endsWith("Z")).toBe(true);
  });
});

describe("buildIngestArrays", () => {
  it("5 本の配列の長さが揃う", () => {
    const arrays = buildIngestArrays(normalizeFixture("hellocycling").stations);
    expect(hasConsistentLengths(arrays)).toBe(true);
    expect(arrays.p_station_ids).toHaveLength(14_835);
  });

  it("並び順が p_station_ids と 1 対 1 で対応する", () => {
    const stations = normalizeFixture("docomo-cycle").stations;
    const arrays = buildIngestArrays(stations);
    stations.forEach((station, index) => {
      expect(arrays.p_station_ids[index]).toBe(station.station_id);
      expect(arrays.p_bikes[index]).toBe(station.bikes);
      expect(arrays.p_docks[index]).toBe(station.docks);
      expect(arrays.p_flags[index]).toBe(station.flags);
      expect(arrays.p_reported_age_s[index]).toBe(station.reported_age_s);
    });
  });

  it("フィードに現れた順を保つ（重複を除いた後の先頭優先の順）", () => {
    const feed = normalizeFixture("docomo-cycle");
    const arrays = buildIngestArrays(feed.stations);
    expect(arrays.p_station_ids[0]).toBe(feed.stations[0]!.station_id);
    expect(arrays.p_station_ids.at(-1)).toBe(feed.stations.at(-1)!.station_id);
  });

  it("station_id に重複が無い（RPC 側の idx 採番が壊れない）", () => {
    const arrays = buildIngestArrays(normalizeFixture("docomo-cycle").stations);
    expect(new Set(arrays.p_station_ids).size).toBe(arrays.p_station_ids.length);
  });

  it("空のポート集合でも 5 本とも空配列になる", () => {
    const arrays = buildIngestArrays([]);
    expect(hasConsistentLengths(arrays)).toBe(true);
    expect(arrays.p_station_ids).toHaveLength(0);
    expect(arrays.p_bikes).toHaveLength(0);
  });

  it("-1 で埋めた密な配列は作らない（密化は RPC の仕事）", () => {
    const arrays = buildIngestArrays(normalizeFixture("hellocycling").stations);
    expect(arrays.p_bikes.includes(-1)).toBe(false);
    expect(arrays.p_reported_age_s.includes(-1)).toBe(false);
  });
});

describe("hasConsistentLengths", () => {
  it("長さがずれていれば false", () => {
    expect(
      hasConsistentLengths({
        p_station_ids: ["a", "b"],
        p_bikes: [1],
        p_docks: [1, 2],
        p_flags: [7, 7],
        p_reported_age_s: [0, 0],
      }),
    ).toBe(false);
  });
});

describe("buildIngestArgs", () => {
  const args = () =>
    buildIngestArgs({
      system_id: "hellocycling",
      feed: normalizeFixture("hellocycling"),
      fetched_at: FETCHED_AT,
      etag: 'W/"400ede-abc"',
      raw_path: "hellocycling/2026/09/06/station_status_1788677493.json.gz",
    });

  it("RPC の引数名と揃っている（§11.3）", () => {
    expect(Object.keys(args()).sort()).toEqual(
      [
        "p_bikes",
        "p_docks",
        "p_etag",
        "p_fetched_at",
        "p_flags",
        "p_observed_at",
        "p_raw_path",
        "p_reported_age_s",
        "p_station_ids",
        "p_system_id",
      ].sort(),
    );
  });

  it("observed_at はフィードの last_updated、fetched_at は取得時刻", () => {
    const built = args();
    expect(built.p_observed_at).toBe("2026-09-06T06:51:33.000Z");
    expect(built.p_fetched_at).toBe("2026-09-06T06:52:00.000Z");
  });

  it("observed_at が raw_path の epoch と一致する（保存物と DB がずれない）", () => {
    const built = args();
    const epochInPath = Number(built.p_raw_path.split("_").at(-1)!.replace(".json.gz", ""));
    expect(built.p_observed_at).toBe(toIsoUtc(epochInPath));
  });

  it("ETag が無ければ null（RPC は null を受け取る）", () => {
    const built = buildIngestArgs({
      system_id: "docomo-cycle",
      feed: normalizeFixture("docomo-cycle"),
      fetched_at: FETCHED_AT,
      etag: null,
      raw_path: "p",
    });
    expect(built.p_etag).toBeNull();
    expect(built.p_system_id).toBe("docomo-cycle");
  });

  it("JSON に直列化できる（PostgREST へ POST の本文で渡すため）", () => {
    const serialized = JSON.stringify(args());
    expect(serialized.length).toBeGreaterThan(0);
    const parsed: unknown = JSON.parse(serialized);
    expect(parsed).toHaveProperty("p_station_ids");
  });

  it("直列化した大きさが Data API の上限に対して余裕がある", () => {
    // 実測：本番の PostgREST は 3.3 MB を受け付ける（W1 プラン §4.1 (d)）
    const bytes = Buffer.byteLength(JSON.stringify(args()), "utf8");
    expect(bytes).toBeLessThan(1_000_000);
  });
});
