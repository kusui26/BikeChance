/**
 * 公開 API が読むビューと絞り込み（`lib/api/view-query.ts`）。
 *
 * `read-port.ts` は supabase-js に渡すだけの薄い層なので、**判断の入る部分はすべてここ**にある。
 * 守りたいのは 3 つ。
 *   * 触れる先が `v1_` のビューだけであること（基底テーブルの名前を公開経路に出さない）
 *   * **bbox の 4 辺が正しい列と演算子に写ること**（南北と東西を入れ替えても型は通る）
 *   * NULL を許す列と許さない列の区別（未取得・未観測を 0 や false にしない）
 */
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  FEEDS_VIEW,
  FEED_COLUMNS,
  STATIONS_VIEW,
  STATION_COLUMNS,
  STATION_ORDER,
  bboxFilters,
  feedRowSchema,
  stationRowSchema,
  systemFilters,
} from "./view-query";

const BBOX = { west: 139.76, south: 35.67, east: 139.78, north: 35.69 };

const stationRow = {
  system_id: "hellocycling",
  station_id: "s-1",
  name: "テスト",
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
};

const feedRow = {
  system_id: "hellocycling",
  display_name: "HELLO CYCLING",
  expected_cadence_s: 300,
  poll_interval_s: 60,
  capacity_is_dynamic: false,
  last_observed_at: "2026-09-07T23:58:00.000Z",
};

describe("触れる先", () => {
  it("ビューの名前は v1_ で始まる", () => {
    expect(FEEDS_VIEW).toBe("v1_feeds");
    expect(STATIONS_VIEW).toBe("v1_stations_current");
  });

  it("公開経路のどこにも基底テーブルの名前が出てこない", () => {
    // 匿名に権限があるのはビューだけ（migration 0018、pgTAP 0010）。
    // 公開側のコードが基底テーブルを名指ししていたら、権限設計と食い違っている
    const dir = fileURLToPath(new URL(".", import.meta.url));
    const sources = readdirSync(dir)
      .filter((name) => name.endsWith(".ts") && !name.endsWith(".test.ts"))
      .map((name) => ({ name, text: readFileSync(`${dir}${name}`, "utf8") }));
    expect(sources.length).toBeGreaterThan(0);
    for (const { name, text } of sources) {
      for (const table of [
        "station_status_latest",
        "station_attributes",
        "feed_state",
        "systems",
      ]) {
        expect(text, `${name} が ${table} を名指ししています`).not.toContain(`"${table}"`);
      }
    }
  });

  it("読む列を明示する（列が増えたときに気づけるように）", () => {
    expect(STATION_COLUMNS).not.toContain("*");
    expect(FEED_COLUMNS).not.toContain("*");
  });

  it("列の一覧はスキーマと一致する", () => {
    expect(STATION_COLUMNS.split(",").sort()).toEqual(Object.keys(stationRowSchema.shape).sort());
    expect(FEED_COLUMNS.split(",").sort()).toEqual(Object.keys(feedRowSchema.shape).sort());
  });
});

describe("bboxFilters", () => {
  it("緯度は south〜north、経度は west〜east に写る", () => {
    expect(bboxFilters(BBOX)).toEqual([
      { op: "gte", column: "lat", value: 35.67 },
      { op: "lte", column: "lat", value: 35.69 },
      { op: "gte", column: "lon", value: 139.76 },
      { op: "lte", column: "lon", value: 139.78 },
    ]);
  });

  it("4 辺すべてを条件にする（1 辺でも落ちると余計なポートが混ざる）", () => {
    const filters = bboxFilters(BBOX);
    expect(filters).toHaveLength(4);
    expect(new Set(filters.map((filter) => `${filter.column}:${filter.op}`)).size).toBe(4);
  });

  it("下限は gte、上限は lte（境界のポートを落とさない）", () => {
    const filters = bboxFilters(BBOX);
    expect(filters.filter((filter) => filter.op === "gte").map((f) => f.value)).toEqual([
      BBOX.south,
      BBOX.west,
    ]);
  });
});

describe("systemFilters", () => {
  it("未指定なら条件を足さない", () => {
    expect(systemFilters(null)).toEqual([]);
  });

  it("指定すれば等値で絞る", () => {
    expect(systemFilters("docomo-cycle")).toEqual([
      { op: "eq", column: "system_id", value: "docomo-cycle" },
    ]);
  });
});

describe("並び", () => {
  it("system_id, station_id の順で固定する", () => {
    expect([...STATION_ORDER]).toEqual(["system_id", "station_id"]);
  });
});

describe("行の検査", () => {
  it("正しい行は通る", () => {
    expect(stationRowSchema.safeParse(stationRow).success).toBe(true);
    expect(feedRowSchema.safeParse(feedRow).success).toBe(true);
  });

  it("属性が未取得（NULL）でも通る", () => {
    expect(stationRowSchema.safeParse({ ...stationRow, name: null, capacity: null }).success).toBe(
      true,
    );
  });

  it("未観測（NULL）でも通る", () => {
    const unseen = { ...stationRow, bikes: null, docks: null, is_renting: null };
    expect(stationRowSchema.safeParse(unseen).success).toBe(true);
  });

  it("座標と is_present は NULL を許さない", () => {
    // bbox で絞った結果しか読まないので座標は必ずある。無い＝ビューか問い合わせの誤り
    expect(stationRowSchema.safeParse({ ...stationRow, lat: null }).success).toBe(false);
    expect(stationRowSchema.safeParse({ ...stationRow, is_present: null }).success).toBe(false);
  });

  it("未知のシステムは通さない", () => {
    expect(stationRowSchema.safeParse({ ...stationRow, system_id: "nope" }).success).toBe(false);
  });

  it("型の違いを見逃さない（文字列の数値は数値ではない）", () => {
    expect(stationRowSchema.safeParse({ ...stationRow, lat: "35.68" }).success).toBe(false);
    expect(stationRowSchema.safeParse({ ...stationRow, bikes: "3" }).success).toBe(false);
  });
});
