import { describe, expect, it } from "vitest";
import {
  MAX_PLAUSIBLE_EPOCH_S,
  SMALLINT_MAX,
  parseStationInformationFeed,
  parseStationStatusFeed,
  stationStatusEntrySchema,
} from "../src/index";
import { readFeedFixture } from "./fixtures";

const validEntry = {
  station_id: "1",
  num_bikes_available: 3,
  num_docks_available: 4,
  is_installed: true,
  is_renting: true,
  is_returning: true,
  last_reported: 1_788_677_493,
};

const feedOf = (stations: readonly unknown[]): Record<string, unknown> => ({
  last_updated: 1_788_677_493,
  ttl: 60,
  version: "2.3",
  data: { stations },
});

describe("実フィードが通る", () => {
  it("HELLO の station_status（14,835 ポート）", () => {
    const result = parseStationStatusFeed(readFeedFixture("hellocycling", "station_status"));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.feed.data.stations).toHaveLength(14_835);
    expect(result.feed.version).toBe("2.3");
  });

  it("ドコモの station_status（重複 11 件を含む 5,812 行）", () => {
    const result = parseStationStatusFeed(readFeedFixture("docomo-cycle", "station_status"));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.feed.data.stations).toHaveLength(5_812);
  });

  it("HELLO の station_information（文字列の vehicle_capacity を含む）", () => {
    const result = parseStationInformationFeed(
      readFeedFixture("hellocycling", "station_information"),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const withStringCapacity = result.feed.data.stations.filter(
      (station) => typeof station.vehicle_capacity === "string",
    );
    expect(withStringCapacity.length).toBeGreaterThan(0);
  });

  it("ドコモの station_information（capacity=0 と BBox 外の座標を含む）", () => {
    const result = parseStationInformationFeed(
      readFeedFixture("docomo-cycle", "station_information"),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const stations = result.feed.data.stations;
    expect(stations.filter((s) => s.capacity === 0).length).toBeGreaterThan(0);
    // 経度 39.55 の異常座標。スキーマは弾かず、判断は利用側に委ねる
    expect(stations.some((s) => s.lon < 122)).toBe(true);
  });
});

describe("未知のフィールドを保つ", () => {
  it("HELLO の vehicle_types_available が残る（GBFS の任意フィールド）", () => {
    const result = parseStationStatusFeed(readFeedFixture("hellocycling", "station_status"));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.feed.data.stations[0]).toHaveProperty("vehicle_types_available");
  });

  it("スキーマに書いていないキーも落とさない", () => {
    const parsed = stationStatusEntrySchema.safeParse({ ...validEntry, future_field: { a: 1 } });
    expect(parsed.success).toBe(true);
    expect(parsed.data).toHaveProperty("future_field");
  });

  it("エンベロープの未知キーも落とさない", () => {
    const result = parseStationStatusFeed({ ...feedOf([validEntry]), unknown_top_level: 1 });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.feed).toHaveProperty("unknown_top_level");
  });
});

describe("壊れた入力を弾く", () => {
  const rejects = (stations: readonly unknown[]): readonly string[] => {
    const result = parseStationStatusFeed(feedOf(stations));
    expect(result.ok).toBe(false);
    return result.ok ? [] : result.issues;
  };

  it("必須キーの欠落", () => {
    const { num_bikes_available: _omitted, ...withoutBikes } = validEntry;
    expect(rejects([withoutBikes]).join()).toContain("num_bikes_available");
  });

  it("smallint に収まらない台数（RPC が失敗する前に気づく）", () => {
    expect(rejects([{ ...validEntry, num_docks_available: SMALLINT_MAX + 1 }]).join()).toContain(
      "num_docks",
    );
  });

  it("station_id の型違い", () => {
    expect(rejects([{ ...validEntry, station_id: 12 }]).join()).toContain("station_id");
  });

  it("空の station_id", () => {
    expect(rejects([{ ...validEntry, station_id: "" }]).join()).toContain("station_id");
  });

  it("真偽値でないフラグ", () => {
    expect(rejects([{ ...validEntry, is_renting: 1 }]).join()).toContain("is_renting");
  });

  it("小数の台数", () => {
    expect(rejects([{ ...validEntry, num_bikes_available: 1.5 }]).join()).toContain("num_bikes");
  });

  it("ミリ秒の last_updated（西暦 57000 年のパスを作らない）", () => {
    const result = parseStationStatusFeed({
      ...feedOf([validEntry]),
      last_updated: MAX_PLAUSIBLE_EPOCH_S + 1,
    });
    expect(result.ok).toBe(false);
  });

  it("stations が配列でない", () => {
    const result = parseStationStatusFeed({ last_updated: 1_788_677_493, data: { stations: {} } });
    expect(result.ok).toBe(false);
  });

  it("null や文字列を渡しても例外にならず ok=false を返す", () => {
    expect(parseStationStatusFeed(null).ok).toBe(false);
    expect(parseStationStatusFeed("not json").ok).toBe(false);
    expect(parseStationStatusFeed(undefined).ok).toBe(false);
  });
});

describe("実データの非標準値を弾かない", () => {
  it("HELLO の負の num_docks_available を受け取る（定員超過ポート。実測 17 件）", () => {
    // ここで弾くと HELLO のフィードが丸ごと取り込めなくなる。0 への丸めは正規化の仕事
    const result = parseStationStatusFeed(feedOf([{ ...validEntry, num_docks_available: -1 }]));
    expect(result.ok).toBe(true);
  });

  it("smallint を外れる負値は弾く（取り込み時に必ず失敗するため）", () => {
    const result = parseStationStatusFeed(
      feedOf([{ ...validEntry, num_docks_available: -SMALLINT_MAX - 1 }]),
    );
    expect(result.ok).toBe(false);
  });
});

describe("検証の失敗はデータを漏らさない", () => {
  it("issues にフィードの値そのものを載せない", () => {
    const result = parseStationStatusFeed(
      feedOf([{ ...validEntry, station_id: "secret-station-name" }, { station_id: "" }]),
    );
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues.join()).not.toContain("secret-station-name");
  });

  it("問題が多くても issues は 10 件までに抑える", () => {
    const broken = Array.from({ length: 50 }, () => ({ station_id: "" }));
    const result = parseStationStatusFeed(feedOf(broken));
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues.length).toBeLessThanOrEqual(10);
  });
});
