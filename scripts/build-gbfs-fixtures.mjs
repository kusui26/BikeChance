/**
 * テスト用フィクスチャを実フィードから作る（W1 プラン §6.4、§5.1 の 30）。
 *
 * 入力は **PR 0 が本番の Storage に保存した生 JSON**。ODPT から取り直すのではなく
 * 実際に収集したものを使う。同じバイト列がテストにも本番にも流れることになる。
 *
 * 縮約の方針：
 *   * `station_status` は**縮約しない**。性能テスト（HELLO 14,835 ポートで 150 ms 未満）と
 *     ゴールデンテストは実物の規模でないと意味がないため。gzip 後は HELLO 110 KB、
 *     ドコモ 37 KB で、リポジトリに置いても負担にならない
 *   * `station_information` は縮約する（HELLO の gzip 891 KB は大きすぎる）。ただし
 *     **データの癖を持つポートは必ず残す**。癖の無い一様なサンプルではテストの意味が薄れる
 *
 * 使い方:
 *   node scripts/build-gbfs-fixtures.mjs <生JSONのディレクトリ> <出力ディレクトリ>
 *
 * 生 JSON は次の名前で置く：
 *   {system}.station_status.json / {system}.station_information.json
 */
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { join } from "node:path";

const SYSTEMS = ["hellocycling", "docomo-cycle"];
const INFORMATION_SAMPLE_SIZE = 1000;

/** 日本の外接矩形。外れた座標は残す価値のある異常値（開発プラン §3.6）。 */
const JAPAN_BBOX = { lat_min: 20, lat_max: 46, lon_min: 122, lon_max: 154 };

const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));

const isOutsideJapan = (station) =>
  typeof station.lat !== "number" ||
  typeof station.lon !== "number" ||
  station.lat < JAPAN_BBOX.lat_min ||
  station.lat > JAPAN_BBOX.lat_max ||
  station.lon < JAPAN_BBOX.lon_min ||
  station.lon > JAPAN_BBOX.lon_max;

/**
 * 残す価値のある「稀な異常」。該当するポートは必ず残す。
 * 全件が持つ性質（HELLO の文字列 vehicle_capacity など）はここに入れない。
 * それは異常ではなくフィードの仕様であり、数件あればテストできる。
 */
const rareQuirks = (station, duplicatedIds, extremes) => {
  const reasons = [];
  if (duplicatedIds.has(station.station_id)) reasons.push("重複 station_id");
  if (isOutsideJapan(station)) reasons.push("日本 BBox 外の座標");
  if (station.capacity === 0) reasons.push("capacity=0（休止扱い）");
  if (extremes.has(station.station_id)) reasons.push("容量の最小・最大");
  if (station.vehicle_capacity !== undefined && !/^\d+$/.test(String(station.vehicle_capacity))) {
    reasons.push("数値でない vehicle_capacity");
  }
  return reasons;
};

/** 全件が持つ性質。数件だけ残せばテストできる。 */
const COMMON_TRAITS = [
  ["文字列の vehicle_capacity（非標準）", (s) => typeof s.vehicle_capacity === "string"],
  ["充電ステーション", (s) => s.is_charging_station === true],
  ["rental_uris を持つ", (s) => s.rental_uris !== undefined],
  ["address を持つ", (s) => typeof s.address === "string"],
  ["region_id を持つ", (s) => s.region_id !== undefined],
];
const COMMON_TRAIT_SAMPLES = 5;

const capacityOf = (station) =>
  typeof station.capacity === "number"
    ? station.capacity
    : Number.parseInt(String(station.vehicle_capacity ?? ""), 10);

/** 容量が最小・最大のポートの id を集める（境界値のテストに使う）。 */
const findExtremes = (stations) => {
  const withCapacity = stations.filter((s) => Number.isFinite(capacityOf(s)));
  if (withCapacity.length === 0) return new Set();
  const sorted = [...withCapacity].sort((a, b) => capacityOf(a) - capacityOf(b));
  return new Set([sorted[0], sorted[sorted.length - 1]].map((s) => s.station_id));
};

const findDuplicatedIds = (stations) => {
  const seen = new Set();
  const duplicated = new Set();
  for (const station of stations) {
    if (seen.has(station.station_id)) duplicated.add(station.station_id);
    seen.add(station.station_id);
  }
  return duplicated;
};

/**
 * 稀な異常を全部残し、全件共通の性質は数件ずつ残し、残りは等間隔で拾う。
 * **元の並び順は保つ**。「重複は先頭を残す」の検証が並び順に依存するため。
 */
const reduceStations = (stations, targetSize) => {
  const duplicatedIds = findDuplicatedIds(stations);
  const extremes = findExtremes(stations);
  const keep = new Set();
  const preserved = new Set();

  stations.forEach((station, index) => {
    const reasons = rareQuirks(station, duplicatedIds, extremes);
    if (reasons.length > 0) {
      keep.add(index);
      for (const reason of reasons) preserved.add(reason);
    }
  });

  for (const [label, matches] of COMMON_TRAITS) {
    const hits = stations.map((s, i) => [s, i]).filter(([s]) => matches(s));
    if (hits.length === 0) continue;
    preserved.add(`${label}（${Math.min(hits.length, COMMON_TRAIT_SAMPLES)}/${hits.length} 件）`);
    for (const [, index] of hits.slice(0, COMMON_TRAIT_SAMPLES)) keep.add(index);
  }

  const remaining = Math.max(1, targetSize - keep.size);
  const step = Math.max(1, Math.floor(stations.length / remaining));
  for (let index = 0; index < stations.length && keep.size < targetSize; index += step) {
    keep.add(index);
  }

  return { kept: stations.filter((_, index) => keep.has(index)), preserved: [...preserved].sort() };
};

const writeFixture = (outDir, name, doc) => {
  const json = JSON.stringify(doc);
  const gz = gzipSync(Buffer.from(json, "utf8"), { level: 9 });
  writeFileSync(join(outDir, `${name}.json.gz`), gz);
  return { raw_bytes: Buffer.byteLength(json, "utf8"), gzip_bytes: gz.byteLength };
};

const main = () => {
  const [sourceDir, outDir] = process.argv.slice(2);
  if (sourceDir === undefined || outDir === undefined) {
    console.error(
      "使い方: node scripts/build-gbfs-fixtures.mjs <生JSONのディレクトリ> <出力ディレクトリ>",
    );
    process.exit(1);
  }
  mkdirSync(outDir, { recursive: true });

  for (const system of SYSTEMS) {
    // station_status は原文のまま
    const status = readJson(join(sourceDir, `${system}.station_status.json`));
    const statusSize = writeFixture(outDir, `${system}.station_status`, status);
    console.log(
      `${system}.station_status      ${status.data.stations.length.toLocaleString()} 件（縮約なし）` +
        ` raw=${statusSize.raw_bytes.toLocaleString()} gzip=${statusSize.gzip_bytes.toLocaleString()}`,
    );

    // station_information は縮約する
    const information = readJson(join(sourceDir, `${system}.station_information.json`));
    const { kept, preserved } = reduceStations(information.data.stations, INFORMATION_SAMPLE_SIZE);
    const reduced = { ...information, data: { ...information.data, stations: kept } };
    const infoSize = writeFixture(outDir, `${system}.station_information`, reduced);
    console.log(
      `${system}.station_information ${kept.length.toLocaleString()} 件` +
        `（元 ${information.data.stations.length.toLocaleString()} 件）` +
        ` raw=${infoSize.raw_bytes.toLocaleString()} gzip=${infoSize.gzip_bytes.toLocaleString()}`,
    );
    console.log(`  残した癖: ${preserved.join(" / ")}`);
  }
};

main();
