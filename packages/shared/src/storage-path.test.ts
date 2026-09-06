import { describe, expect, it } from "vitest";
import { rawObjectPath } from "./storage-path";

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
