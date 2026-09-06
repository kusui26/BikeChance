/**
 * ゴールデンテスト（W1 プラン §6.4、開発プラン §12.3 の 3）。
 *
 * 固定フィクスチャから作った RPC 引数の SHA-256 を固定する。リファクタで出力が
 * 1 バイトでも変われば落ちる。正規化の規則（重複の扱い、フラグのビット位置、丸め方）は
 * そのまま DB に入る値なので、意図しない変更は静かな汚染になる。
 *
 * **期待値は、このパッケージとは別に素の JavaScript で書き直した実装から得た。**
 * 自分の出力をそのまま写すと「今の実装がこう出す」ことしか言えないが、独立した実装と
 * 一致するなら「正規化の規則そのものに合意がある」ことまで言える。
 *
 * ハッシュが変わったときは、まず**変えるつもりだったか**を確かめること。
 * フィクスチャを作り直したなら当然変わるので、測り直して書き換える。
 */
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  buildIngestArgs,
  normalizeStationStatus,
  parseStationStatusFeed,
  type IngestArgs,
} from "../src/index";
import { FIXTURE_DATE, readFeedFixture } from "./fixtures";

/** 取得時刻・ETag・保存パスは呼び出し側の値なので、ハッシュを安定させるため固定する。 */
const FETCHED_AT = new Date("2026-09-06T06:52:00.000Z");

const argsFor = (system_id: "hellocycling" | "docomo-cycle"): IngestArgs => {
  const result = parseStationStatusFeed(readFeedFixture(system_id, "station_status"));
  if (!result.ok) throw new Error(result.issues.join());
  return buildIngestArgs({
    system_id,
    feed: normalizeStationStatus(result.feed),
    fetched_at: FETCHED_AT,
    etag: null,
    raw_path: `${system_id}/golden`,
  });
};

const sha256 = (value: unknown): string =>
  createHash("sha256").update(JSON.stringify(value), "utf8").digest("hex");

/** `fixtures/gbfs/2026-09-06` に対する期待値。フィクスチャを作り直したら測り直す。 */
const GOLDEN = {
  hellocycling: {
    station_count: 14_835,
    args: "0c2f9da386fb4871efb6827a3f0a93efdccc659d227f319d935358eef3bafa63",
    station_ids: "31f1fa810da7c9ef8565f3461c4a40a7917ac7fbf97f363cb2c01a090a444890",
    bikes: "cdc1ab1d2171c7e97f3456f186b7870a7e022dec72b4b02a8421ea2dc5cdbbfb",
    docks: "a77eebf614b7b36a3ad1bdd36ae65c279e073fcd186959dbfa805fc1b5a67270",
    flags: "f4dbedbefe8041eec6d9a17e5f75dc4a92fb7f72cd0ff0e41f3fda8958ea9687",
    reported_age_s: "8c3546966951239104cda85a46b592d69b9a91a43882ffdb7cdb531138f940f5",
  },
  "docomo-cycle": {
    station_count: 5_801,
    args: "9d8b09344ec3b315b486674390488a0d8b88ef0c3a8e04f6157225ed83c50bd4",
    station_ids: "0a1d640c42b3b16ab1a711c7595bd83578c489bd1fb5a3de975f7273b32bf593",
    bikes: "a72bdac9bde5251374ecc7ba97f4584b3003a8c9a492a180602fd8f92a75e5b5",
    docks: "2f483c0a2e582c811c7330e7c45383d39a44dfd357508d5377f926348b8a88b4",
    flags: "e7142dd0c7d8d32d888909647dd181eb55ce81e4d1ddcda81f616444fc1a292f",
    reported_age_s: "55a98af96655bd597e40f435b7dfc7a90dd038e886beb242f86aec5f0b235f39",
  },
} as const;

describe(`ゴールデン（fixtures/gbfs/${FIXTURE_DATE}）`, () => {
  for (const system_id of ["hellocycling", "docomo-cycle"] as const) {
    const expected = GOLDEN[system_id];

    it(`${system_id}: ポート数と引数全体のハッシュが変わらない`, () => {
      const args = argsFor(system_id);
      expect(args.p_station_ids).toHaveLength(expected.station_count);
      expect(sha256(args)).toBe(expected.args);
    });

    it(`${system_id}: 配列ごとのハッシュが変わらない（どこが変わったか分かるように）`, () => {
      const args = argsFor(system_id);
      expect({
        station_ids: sha256(args.p_station_ids),
        bikes: sha256(args.p_bikes),
        docks: sha256(args.p_docks),
        flags: sha256(args.p_flags),
        reported_age_s: sha256(args.p_reported_age_s),
      }).toEqual({
        station_ids: expected.station_ids,
        bikes: expected.bikes,
        docks: expected.docks,
        flags: expected.flags,
        reported_age_s: expected.reported_age_s,
      });
    });
  }

  it("同じ入力からは何度でも同じ出力が出る", () => {
    expect(sha256(argsFor("docomo-cycle"))).toBe(sha256(argsFor("docomo-cycle")));
  });
});
