import { describe, expect, it } from "vitest";
import {
  clampCount,
  FLAG_INSTALLED,
  FLAG_RENTING,
  FLAG_RETURNING,
  MAX_REPORTED_AGE_S,
  hasWarnings,
  normalizeStationStatus,
  parseStationStatusFeed,
  toFlags,
  toReportedAgeSeconds,
  type StationStatusFeed,
} from "../src/index";
import { readFeedFixture } from "./fixtures";

const OBSERVED_AT_S = 1_788_677_493;

const entry = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  station_id: "1",
  num_bikes_available: 3,
  num_docks_available: 4,
  is_installed: true,
  is_renting: true,
  is_returning: true,
  last_reported: OBSERVED_AT_S,
  ...overrides,
});

const feedWith = (stations: readonly Record<string, unknown>[]): StationStatusFeed => {
  const result = parseStationStatusFeed({
    last_updated: OBSERVED_AT_S,
    ttl: 60,
    version: "2.3",
    data: { stations },
  });
  if (!result.ok) throw new Error(`フィクスチャが不正: ${result.issues.join()}`);
  return result.feed;
};

const normalizeFixture = (system_id: "hellocycling" | "docomo-cycle") => {
  const result = parseStationStatusFeed(readFeedFixture(system_id, "station_status"));
  if (!result.ok) throw new Error(result.issues.join());
  return normalizeStationStatus(result.feed);
};

describe("toFlags", () => {
  it("すべて真なら 7", () => {
    expect(toFlags(feedWith([entry()]).data.stations[0]!)).toBe(7);
  });

  it("8 通りすべてがビット和になる", () => {
    for (const installed of [false, true]) {
      for (const renting of [false, true]) {
        for (const returning of [false, true]) {
          const station = feedWith([
            entry({ is_installed: installed, is_renting: renting, is_returning: returning }),
          ]).data.stations[0]!;
          const expected =
            (installed ? FLAG_INSTALLED : 0) +
            (renting ? FLAG_RENTING : 0) +
            (returning ? FLAG_RETURNING : 0);
          expect(toFlags(station)).toBe(expected);
        }
      }
    }
  });

  it("欠損を表す -1 と衝突しない（0〜7 の範囲に収まる）", () => {
    const station = feedWith([
      entry({ is_installed: false, is_renting: false, is_returning: false }),
    ]).data.stations[0]!;
    expect(toFlags(station)).toBe(0);
    expect(toFlags(station)).toBeGreaterThanOrEqual(0);
  });
});

describe("toReportedAgeSeconds", () => {
  it("経過秒を返す", () => {
    expect(toReportedAgeSeconds(1000, 958)).toBe(42);
  });

  it("同時刻なら 0", () => {
    expect(toReportedAgeSeconds(1000, 1000)).toBe(0);
  });

  it("負値は 0 に丸める（-1 は欠損専用のため衝突させない）", () => {
    expect(toReportedAgeSeconds(1000, 1100)).toBe(0);
  });

  it("上限を超えたら smallint の最大に丸める", () => {
    expect(toReportedAgeSeconds(1_000_000, 0)).toBe(MAX_REPORTED_AGE_S);
    expect(toReportedAgeSeconds(MAX_REPORTED_AGE_S + 1, 0)).toBe(MAX_REPORTED_AGE_S);
  });

  it("ちょうど上限は丸めない", () => {
    expect(toReportedAgeSeconds(MAX_REPORTED_AGE_S, 0)).toBe(MAX_REPORTED_AGE_S);
  });
});

describe("重複した station_id", () => {
  it("先頭を残す", () => {
    const normalized = normalizeStationStatus(
      feedWith([
        entry({ station_id: "a", num_bikes_available: 1 }),
        entry({ station_id: "a", num_bikes_available: 9 }),
      ]),
    );
    expect(normalized.stations).toHaveLength(1);
    expect(normalized.stations[0]!.bikes).toBe(1);
  });

  it("取り込む値が同じなら exact_duplicates に数える", () => {
    const normalized = normalizeStationStatus(
      feedWith([entry({ station_id: "a" }), entry({ station_id: "a" })]),
    );
    expect(normalized.warnings.exact_duplicates).toBe(1);
    expect(normalized.warnings.conflicting_duplicates).toBe(0);
  });

  it("取り込む値が違えば conflicting_duplicates に数える（情報を捨てている印）", () => {
    const normalized = normalizeStationStatus(
      feedWith([
        entry({ station_id: "a", num_bikes_available: 1 }),
        entry({ station_id: "a", num_bikes_available: 2 }),
      ]),
    );
    expect(normalized.warnings.conflicting_duplicates).toBe(1);
    expect(normalized.warnings.exact_duplicates).toBe(0);
  });

  it("取り込まない項目だけが違う重複は「同じ」とみなす", () => {
    // vehicle_types_available は status_snapshots に入れない。値の差は無視してよい
    const normalized = normalizeStationStatus(
      feedWith([
        entry({ station_id: "a", vehicle_types_available: [{ vehicle_type_id: "2", count: 1 }] }),
        entry({ station_id: "a", vehicle_types_available: [] }),
      ]),
    );
    expect(normalized.warnings.exact_duplicates).toBe(1);
    expect(normalized.warnings.conflicting_duplicates).toBe(0);
  });

  it("3 件以上の重複も先頭だけ残る", () => {
    const normalized = normalizeStationStatus(
      feedWith([
        entry({ station_id: "a", num_bikes_available: 1 }),
        entry({ station_id: "a", num_bikes_available: 1 }),
        entry({ station_id: "a", num_bikes_available: 1 }),
      ]),
    );
    expect(normalized.stations).toHaveLength(1);
    expect(normalized.warnings.exact_duplicates).toBe(2);
  });
});

describe("last_reported の欠落", () => {
  it("observed_at と同時刻とみなして 0 にする", () => {
    const { last_reported: _omitted, ...withoutReported } = entry();
    const normalized = normalizeStationStatus(feedWith([withoutReported]));
    expect(normalized.stations[0]!.reported_age_s).toBe(0);
    expect(normalized.warnings.missing_last_reported).toBe(1);
  });
});

describe("負の台数・返却枠（HELLO の定員超過ポート）", () => {
  it("0 に丸める（-1 は欠損の予約値なので衝突させない）", () => {
    const normalized = normalizeStationStatus(
      feedWith([entry({ num_docks_available: -1, num_bikes_available: 4 })]),
    );
    expect(normalized.stations[0]!.docks).toBe(0);
    expect(normalized.stations[0]!.bikes).toBe(4);
  });

  it("丸めた件数を警告に残す", () => {
    const normalized = normalizeStationStatus(
      feedWith([
        entry({ station_id: "a", num_docks_available: -1 }),
        entry({ station_id: "b", num_docks_available: 3 }),
      ]),
    );
    expect(normalized.warnings.clamped_negative_counts).toBe(1);
  });

  it("clampCount は非負をそのまま返す", () => {
    expect(clampCount(0)).toBe(0);
    expect(clampCount(7)).toBe(7);
    expect(clampCount(-1)).toBe(0);
    expect(clampCount(-99)).toBe(0);
  });
});

describe("警告", () => {
  it("何も無ければすべて 0", () => {
    const normalized = normalizeStationStatus(feedWith([entry()]));
    expect(normalized.warnings).toEqual({
      exact_duplicates: 0,
      conflicting_duplicates: 0,
      missing_last_reported: 0,
      clamped_reported_age: 0,
      clamped_negative_counts: 0,
    });
    expect(hasWarnings(normalized.warnings)).toBe(false);
  });

  it("時計ずれを clamped_reported_age に数える", () => {
    const normalized = normalizeStationStatus(
      feedWith([entry({ station_id: "a", last_reported: OBSERVED_AT_S + 30 })]),
    );
    expect(normalized.warnings.clamped_reported_age).toBe(1);
    expect(normalized.stations[0]!.reported_age_s).toBe(0);
    expect(hasWarnings(normalized.warnings)).toBe(true);
  });
});

describe("空のフィード", () => {
  it("ポートが 0 件でも例外にならない", () => {
    const normalized = normalizeStationStatus(feedWith([]));
    expect(normalized.stations).toHaveLength(0);
    expect(normalized.observed_at_s).toBe(OBSERVED_AT_S);
    expect(hasWarnings(normalized.warnings)).toBe(false);
  });
});

describe("実データ", () => {
  it("HELLO は重複が無く 14,835 ポートのまま", () => {
    const normalized = normalizeFixture("hellocycling");
    expect(normalized.stations).toHaveLength(14_835);
    expect(normalized.warnings.exact_duplicates).toBe(0);
    expect(normalized.warnings.conflicting_duplicates).toBe(0);
    expect(normalized.warnings.missing_last_reported).toBe(0);
  });

  it("HELLO の定員超過ポート 17 件が 0 に丸められる", () => {
    const normalized = normalizeFixture("hellocycling");
    expect(normalized.warnings.clamped_negative_counts).toBe(17);
    // 丸めた後は負値が残っていない（-1 が欠損と衝突しない）
    expect(normalized.stations.every((s) => s.bikes >= 0 && s.docks >= 0)).toBe(true);
  });

  it("ドコモには負値が無い", () => {
    expect(normalizeFixture("docomo-cycle").warnings.clamped_negative_counts).toBe(0);
  });

  it("ドコモは完全重複 11 件が除かれて 5,801 ポートになる", () => {
    const normalized = normalizeFixture("docomo-cycle");
    expect(normalized.stations).toHaveLength(5_801);
    expect(normalized.warnings.exact_duplicates).toBe(11);
    expect(normalized.warnings.conflicting_duplicates).toBe(0);
  });

  it("ドコモの last_reported は全件 last_updated と同値なので age は 0", () => {
    const normalized = normalizeFixture("docomo-cycle");
    expect(normalized.stations.every((s) => s.reported_age_s === 0)).toBe(true);
  });

  it("HELLO はポート単位で鮮度が分かる（age に幅がある）", () => {
    const normalized = normalizeFixture("hellocycling");
    const ages = new Set(normalized.stations.map((s) => s.reported_age_s));
    expect(ages.size).toBeGreaterThan(1);
    expect(Math.max(...ages)).toBeLessThan(300); // 5 分周期を超えない
  });

  it("実データの値がすべて smallint の範囲に収まる", () => {
    for (const system of ["hellocycling", "docomo-cycle"] as const) {
      for (const station of normalizeFixture(system).stations) {
        expect(station.bikes).toBeGreaterThanOrEqual(0);
        expect(station.docks).toBeGreaterThanOrEqual(0);
        expect(station.bikes).toBeLessThanOrEqual(MAX_REPORTED_AGE_S);
        expect(station.docks).toBeLessThanOrEqual(MAX_REPORTED_AGE_S);
        expect(station.flags).toBeGreaterThanOrEqual(0);
        expect(station.flags).toBeLessThanOrEqual(7);
      }
    }
  });

  it("休止中のポート（is_renting=false）が実在する", () => {
    const normalized = normalizeFixture("hellocycling");
    const halted = normalized.stations.filter((s) => (s.flags & FLAG_RENTING) === 0);
    expect(halted.length).toBeGreaterThan(0);
    // 実測 158 件。フラグの立て方が逆になっていればここで気づく
    expect(halted.length).toBeLessThan(normalized.stations.length / 2);
  });
});
