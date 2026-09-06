import { describe, expect, it } from "vitest";
import { readLastUpdated } from "./feed-timestamp";

const encode = (value: unknown): Uint8Array =>
  new TextEncoder().encode(typeof value === "string" ? value : JSON.stringify(value));

/** 2026-09-06T05:00:00Z 相当。実フィードが返す桁数に合わせる。 */
const VALID_EPOCH_S = 1_788_670_800;

describe("readLastUpdated", () => {
  it("GBFS の last_updated を秒として読む", () => {
    const body = encode({ last_updated: VALID_EPOCH_S, ttl: 60, data: { stations: [] } });
    expect(readLastUpdated(body)).toBe(VALID_EPOCH_S);
  });

  it("壊れた JSON では null を返す（保存は続けられるようにする）", () => {
    expect(readLastUpdated(encode("{ this is not json"))).toBeNull();
  });

  it("空のバイト列でも例外にならない", () => {
    expect(readLastUpdated(new Uint8Array())).toBeNull();
  });

  it("last_updated が無ければ null", () => {
    expect(readLastUpdated(encode({ ttl: 60 }))).toBeNull();
  });

  it("配列やスカラの JSON でも例外にならない", () => {
    expect(readLastUpdated(encode([1, 2, 3]))).toBeNull();
    expect(readLastUpdated(encode(42))).toBeNull();
    expect(readLastUpdated(encode(null))).toBeNull();
  });

  it("ミリ秒が来たら弾く（年 57000 のパスを作らない）", () => {
    expect(readLastUpdated(encode({ last_updated: VALID_EPOCH_S * 1000 }))).toBeNull();
  });

  it("小数・負値・0・文字列は弾く", () => {
    expect(readLastUpdated(encode({ last_updated: VALID_EPOCH_S + 0.5 }))).toBeNull();
    expect(readLastUpdated(encode({ last_updated: -1 }))).toBeNull();
    expect(readLastUpdated(encode({ last_updated: 0 }))).toBeNull();
    expect(readLastUpdated(encode({ last_updated: String(VALID_EPOCH_S) }))).toBeNull();
  });

  it("2020 年より前の値は弾く（時計ずれや秒/ミリ秒の取り違え）", () => {
    expect(readLastUpdated(encode({ last_updated: 1 }))).toBeNull();
  });
});
