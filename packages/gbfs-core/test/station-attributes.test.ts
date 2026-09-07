/**
 * station_information の正規化（W1 プラン §6.9）。
 * 実データのフィクスチャ（`fixtures/gbfs/`）と、境界値の合成入力で検証する。
 */
import { describe, expect, it } from "vitest";
import {
  hasAttributeWarnings,
  isInsideJapan,
  normalizeStationInformation,
  parseStationInformationFeed,
  toCapacity,
  type StationInformationFeed,
} from "../src";
import { readFeedFixture } from "./fixtures";

const parse = (input: unknown): StationInformationFeed => {
  const result = parseStationInformationFeed(input);
  if (!result.ok) throw new Error(result.issues.join(" / "));
  return result.feed;
};

/** 合成フィード。境界値だけを狙って書く。 */
const feedOf = (stations: readonly Record<string, unknown>[]): StationInformationFeed =>
  parse({ last_updated: 1788678460, data: { stations } });

const entry = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  station_id: "s1",
  name: "テストポート",
  lat: 35.68,
  lon: 139.76,
  ...over,
});

describe("toCapacity", () => {
  it("ドコモの数値 capacity をそのまま読む", () => {
    expect(toCapacity(feedOf([entry({ capacity: 12 })]).data.stations[0]!).capacity).toBe(12);
  });

  it("HELLO の文字列 vehicle_capacity を数値にする", () => {
    expect(toCapacity(feedOf([entry({ vehicle_capacity: "8" })]).data.stations[0]!).capacity).toBe(
      8,
    );
  });

  it("capacity があれば vehicle_capacity より優先する", () => {
    const station = feedOf([entry({ capacity: 3, vehicle_capacity: "99" })]).data.stations[0]!;
    expect(toCapacity(station).capacity).toBe(3);
  });

  it("どちらも無ければ null（バーチャルポートは異常ではない）", () => {
    const outcome = toCapacity(feedOf([entry()]).data.stations[0]!);
    expect(outcome.capacity).toBeNull();
    expect(outcome.missing).toBe(true);
  });

  it("数値にできない文字列は null にして unparsable として数える", () => {
    const outcome = toCapacity(feedOf([entry({ vehicle_capacity: "不明" })]).data.stations[0]!);
    expect(outcome.capacity).toBeNull();
    expect(outcome.unparsable).toBe(true);
  });

  it("smallint を超える値は丸めて clamped にする", () => {
    const outcome = toCapacity(feedOf([entry({ capacity: 40000 })]).data.stations[0]!);
    expect(outcome.capacity).toBe(32767);
    expect(outcome.clamped).toBe(true);
  });

  it("小数は切り捨てる", () => {
    expect(
      toCapacity(feedOf([entry({ vehicle_capacity: "6.9" })]).data.stations[0]!).capacity,
    ).toBe(6);
  });
});

describe("isInsideJapan", () => {
  it("東京は中", () => expect(isInsideJapan(35.68, 139.76)).toBe(true));
  it("石垣は中", () => expect(isInsideJapan(24.34, 124.16)).toBe(true));
  it("経度と緯度を取り違えた座標は外", () => expect(isInsideJapan(139.76, 35.68)).toBe(false));
  it("0,0 は外", () => expect(isInsideJapan(0, 0)).toBe(false));
});

describe("normalizeStationInformation", () => {
  it("重複した station_id は先頭を残す", () => {
    const feed = feedOf([
      entry({ station_id: "a", name: "先" }),
      entry({ station_id: "a", name: "先" }),
      entry({ station_id: "a", name: "後" }),
      entry({ station_id: "b" }),
    ]);
    const result = normalizeStationInformation(feed);
    expect(result.stations).toHaveLength(2);
    expect(result.stations[0]?.name).toBe("先");
    expect(result.warnings.exact_duplicates).toBe(1);
    expect(result.warnings.conflicting_duplicates).toBe(1);
  });

  it("フィードに現れた順を保つ", () => {
    const result = normalizeStationInformation(
      feedOf([entry({ station_id: "z" }), entry({ station_id: "a" })]),
    );
    expect(result.stations.map((s) => s.station_id)).toEqual(["z", "a"]);
  });

  it("raw に未知フィールドを保全する", () => {
    const result = normalizeStationInformation(
      feedOf([entry({ address: "東京都…", rental_uris: { android: "x" } })]),
    );
    expect(result.stations[0]?.raw["address"]).toBe("東京都…");
  });

  it("範囲外の座標を数える", () => {
    const result = normalizeStationInformation(
      feedOf([entry({ station_id: "a" }), entry({ station_id: "b", lat: 0, lon: 0 })]),
    );
    expect(result.warnings.outside_japan).toBe(1);
  });

  it("observed_at_s はフィードの last_updated", () => {
    expect(normalizeStationInformation(feedOf([entry()])).observed_at_s).toBe(1788678460);
  });

  it("空のフィードでも壊れない", () => {
    const result = normalizeStationInformation(feedOf([]));
    expect(result.stations).toHaveLength(0);
    expect(hasAttributeWarnings(result.warnings)).toBe(false);
  });
});

describe("実データ（fixtures）", () => {
  it("HELLO は vehicle_capacity から容量が読める", () => {
    const result = normalizeStationInformation(
      parse(readFeedFixture("hellocycling", "station_information")),
    );
    expect(result.stations.length).toBeGreaterThan(900);
    expect(result.warnings.unparsable_capacity).toBe(0);
    const withCapacity = result.stations.filter((s) => s.capacity !== null);
    expect(withCapacity.length).toBeGreaterThan(result.stations.length * 0.9);
    expect(result.stations.every((s) => s.capacity === null || s.capacity >= 0)).toBe(true);
  });

  it("ドコモは capacity が数値で、0 のポートも実在する", () => {
    const result = normalizeStationInformation(
      parse(readFeedFixture("docomo-cycle", "station_information")),
    );
    expect(result.stations.length).toBeGreaterThan(900);
    expect(result.warnings.unparsable_capacity).toBe(0);
    expect(result.stations.some((s) => s.capacity === 0)).toBe(true);
  });

  it("HELLO の座標はすべて日本の中にある", () => {
    const result = normalizeStationInformation(
      parse(readFeedFixture("hellocycling", "station_information")),
    );
    expect(result.warnings.outside_japan).toBe(0);
  });

  it("ドコモには経度が壊れたポートが 1 件あり、geo_suspect が立つ", () => {
    // 4826「アートフォーラムあざみ野」は横浜市青葉区。経度は 139.553764 のはずが
    // 39.553764 になっていて、先頭の 1 が落ちている（開発プラン §14 で予見済み）。
    // 提供側が直せばこのテストが落ちるので、そのとき期待値を更新する
    const result = normalizeStationInformation(
      parse(readFeedFixture("docomo-cycle", "station_information")),
    );
    expect(result.warnings.outside_japan).toBe(1);
    const suspects = result.stations.filter((s) => s.geo_suspect);
    expect(suspects).toHaveLength(1);
    expect(suspects[0]?.station_id).toBe("4826");
    expect(suspects[0]?.lon).toBeLessThan(122);
  });

  it("範囲内のポートには geo_suspect が立たない", () => {
    const result = normalizeStationInformation(
      parse(readFeedFixture("hellocycling", "station_information")),
    );
    expect(result.stations.every((s) => !s.geo_suspect)).toBe(true);
  });

  it("station_id が一意（正規化後）", () => {
    for (const system of ["hellocycling", "docomo-cycle"] as const) {
      const result = normalizeStationInformation(
        parse(readFeedFixture(system, "station_information")),
      );
      const ids = new Set(result.stations.map((s) => s.station_id));
      expect(ids.size).toBe(result.stations.length);
    }
  });
});
