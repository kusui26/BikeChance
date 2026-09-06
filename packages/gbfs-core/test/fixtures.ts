/**
 * フィクスチャの読み込み。テストから共通で使う。
 * 実データの出どころと縮約の方針は `fixtures/gbfs/README.md` を参照。
 */
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { fileURLToPath } from "node:url";
import type { FeedName, SystemId } from "@bikechance/shared";

/** フィクスチャを採取した日（Storage のパスと同じ UTC 日）。 */
export const FIXTURE_DATE = "2026-09-06";

const fixtureDir = fileURLToPath(
  new URL(`../../../fixtures/gbfs/${FIXTURE_DATE}/`, import.meta.url),
);

/** gzip のまま置いてあるフィクスチャを展開して JSON にする。 */
export const readFeedFixture = (system_id: SystemId, feed: FeedName): unknown => {
  const gz = readFileSync(`${fixtureDir}${system_id}.${feed}.json.gz`);
  return JSON.parse(gunzipSync(gz).toString("utf8"));
};

/** 圧縮したままのバイト列（サイズを測るテスト用）。 */
export const readFeedFixtureBytes = (system_id: SystemId, feed: FeedName): Buffer =>
  readFileSync(`${fixtureDir}${system_id}.${feed}.json.gz`);
