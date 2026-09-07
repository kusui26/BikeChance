import { describe, expect, it } from "vitest";
import { parseRawObjectName, rawObjectPath, utcDayPathsBetween } from "./storage-path";

/** 2026-09-06T05:00:00Z（JST では 09-06 14:00）。 */
const EPOCH_S = 1_788_670_800;

describe("rawObjectPath", () => {
  it("§11.5 の規約どおりのパスを作る", () => {
    const path = rawObjectPath({
      system_id: "hellocycling",
      feed: "station_status",
      epoch_s: EPOCH_S,
    });
    expect(path).toBe(`hellocycling/2026/09/06/station_status_${EPOCH_S}.json.gz`);
  });

  it("バケット名は含めない（supabase-js は from() で指定するため）", () => {
    const path = rawObjectPath({
      system_id: "docomo-cycle",
      feed: "station_status",
      epoch_s: EPOCH_S,
    });
    expect(path.startsWith("gbfs-raw/")).toBe(false);
  });

  it("月日をゼロ詰めする", () => {
    // 2026-01-02T03:04:05Z
    const path = rawObjectPath({
      system_id: "docomo-cycle",
      feed: "station_information",
      epoch_s: 1_767_326_645,
    });
    expect(path).toContain("/2026/01/02/");
  });

  it("日付は UTC で切る（JST で日付が変わる時刻でも UTC の日に入る）", () => {
    // 2026-09-06T23:30:00Z は JST では 09-07 08:30 だが、パスは UTC の 09/06
    const path = rawObjectPath({
      system_id: "hellocycling",
      feed: "station_status",
      epoch_s: 1_788_737_400,
    });
    expect(path).toContain("/2026/09/06/");
  });

  it("同じ観測は同じパスに写像される（409 で重複が畳まれる前提）", () => {
    const params = { system_id: "hellocycling", feed: "station_status", epoch_s: EPOCH_S } as const;
    expect(rawObjectPath(params)).toBe(rawObjectPath(params));
  });

  it("システムとフィードでパスが分かれる", () => {
    const status = rawObjectPath({
      system_id: "hellocycling",
      feed: "station_status",
      epoch_s: EPOCH_S,
    });
    const information = rawObjectPath({
      system_id: "hellocycling",
      feed: "station_information",
      epoch_s: EPOCH_S,
    });
    expect(status).not.toBe(information);
  });
});

describe("parseRawObjectName", () => {
  it("rawObjectPath の逆向きになる（書く側と読む側が一致する）", () => {
    const path = rawObjectPath({
      system_id: "hellocycling",
      feed: "station_status",
      epoch_s: 1_788_678_460,
    });
    const name = path.split("/").at(-1) ?? "";
    expect(parseRawObjectName(name)).toEqual({ feed: "station_status", epoch_s: 1_788_678_460 });
  });

  it("station_information も読める", () => {
    expect(parseRawObjectName("station_information_1788678462.json.gz")).toEqual({
      feed: "station_information",
      epoch_s: 1_788_678_462,
    });
  });

  it("知らないフィード名は null", () => {
    expect(parseRawObjectName("free_bike_status_1788678460.json.gz")).toBeNull();
  });

  it("形式が違えば null（例外にしない）", () => {
    expect(parseRawObjectName("station_status.json.gz")).toBeNull();
    expect(parseRawObjectName("station_status_abc.json.gz")).toBeNull();
    expect(parseRawObjectName("station_status_1788678460.json")).toBeNull();
    expect(parseRawObjectName("")).toBeNull();
  });

  it("0 や負の epoch は null", () => {
    expect(parseRawObjectName("station_status_0.json.gz")).toBeNull();
  });
});

describe("utcDayPathsBetween", () => {
  const at = (iso: string): Date => new Date(iso);

  it("両端を含む", () => {
    expect(utcDayPathsBetween(at("2026-09-06T00:00:00Z"), at("2026-09-08T00:00:00Z"))).toEqual([
      "2026/09/06",
      "2026/09/07",
      "2026/09/08",
    ]);
  });

  it("同じ日なら 1 つ", () => {
    expect(utcDayPathsBetween(at("2026-09-06T23:59:59Z"), at("2026-09-06T00:00:01Z"))).toEqual([
      "2026/09/06",
    ]);
  });

  it("月をまたぐ", () => {
    expect(utcDayPathsBetween(at("2026-09-30T00:00:00Z"), at("2026-10-01T00:00:00Z"))).toEqual([
      "2026/09/30",
      "2026/10/01",
    ]);
  });

  it("年をまたぐ", () => {
    expect(utcDayPathsBetween(at("2026-12-31T00:00:00Z"), at("2027-01-01T00:00:00Z"))).toEqual([
      "2026/12/31",
      "2027/01/01",
    ]);
  });

  it("時刻は UTC で切る（JST の日付ではない）", () => {
    // 2026-09-08T23:30:00Z は JST では 09-09 の朝。UTC の 09-08 として扱う
    expect(utcDayPathsBetween(at("2026-09-08T23:30:00Z"), at("2026-09-08T23:30:00Z"))).toEqual([
      "2026/09/08",
    ]);
  });

  it("終了日が開始日より前なら弾く", () => {
    expect(() =>
      utcDayPathsBetween(at("2026-09-08T00:00:00Z"), at("2026-09-06T00:00:00Z")),
    ).toThrow();
  });
});
