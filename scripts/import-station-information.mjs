/**
 * 段 0 で手で保存した station_information を、あとから属性履歴に取り込む（W1 プラン §6.9）。
 *
 * PR F が動き出すまで `station_information` を取り込むものが無かった。段 0 で各システム
 * 1 回だけ手で取得して Storage に置いてあるので、それを**日次同期より先に**入れると、
 * 属性履歴の起点がその日になり、W1 中の属性変化を失わない。
 *
 * **順番が大事。** SCD2 は「有効行より新しい取り込み」しか受け付けない（古い時刻の
 * 取り込みは `n_skipped_older` に出るだけで反映されない）。ベースラインを先に入れてから
 * 日次同期を動かすこと。逆順にすると起点が今日になる。
 *
 * 取得時刻はファイル名の epoch（フィードの last_updated）をそのまま使う。
 *
 * 使い方:
 *   node scripts/import-station-information.mjs <環境ファイル> <Storage のパス>
 *   node scripts/import-station-information.mjs .env \
 *     hellocycling/2026/09/06/station_information_1788678460.json.gz
 */
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

const SMALLINT_MAX = 32767;
const JAPAN_BBOX = { lat_min: 20, lat_max: 46, lon_min: 122, lon_max: 154 };

const readEnv = (path) =>
  Object.fromEntries(
    readFileSync(path, "utf8")
      .split("\n")
      .filter((line) => line.includes("=") && !line.trimStart().startsWith("#"))
      .map((line) => {
        const at = line.indexOf("=");
        return [line.slice(0, at).trim(), line.slice(at + 1).trim()];
      }),
  );

const authHeaders = (env) => ({
  apikey: env.SUPABASE_SECRET_KEY,
  Authorization: `Bearer ${env.SUPABASE_SECRET_KEY}`,
});

const downloadFeed = async (env, path) => {
  const response = await fetch(`${env.SUPABASE_URL}/storage/v1/object/gbfs-raw/${path}`, {
    headers: authHeaders(env),
  });
  if (!response.ok) {
    throw new Error(`Storage ${path} が ${response.status}`);
  }
  return JSON.parse(gunzipSync(Buffer.from(await response.arrayBuffer())).toString("utf8"));
};

/**
 * `packages/gbfs-core` と同じ規則。ここで書き直しているのは、スクリプトが
 * TypeScript のビルド成果物に依存しないようにするため（他のスクリプトと同じ方針）。
 */
const toCapacity = (entry) => {
  const source = entry.capacity ?? entry.vehicle_capacity;
  if (source === undefined || source === null) return null;
  const parsed = typeof source === "number" ? source : Number(source);
  if (!Number.isFinite(parsed)) return null;
  const truncated = Math.trunc(parsed);
  if (truncated < 0) return 0;
  return truncated > SMALLINT_MAX ? SMALLINT_MAX : truncated;
};

const isInsideJapan = (lat, lon) =>
  lat >= JAPAN_BBOX.lat_min &&
  lat <= JAPAN_BBOX.lat_max &&
  lon >= JAPAN_BBOX.lon_min &&
  lon <= JAPAN_BBOX.lon_max;

/** 重複した station_id は先頭を残す（§11.1）。 */
const toRows = (feed) => {
  const byId = new Map();
  for (const entry of feed.data.stations) {
    if (byId.has(entry.station_id)) continue;
    byId.set(entry.station_id, {
      station_id: entry.station_id,
      name: entry.name,
      lat: entry.lat,
      lon: entry.lon,
      capacity: toCapacity(entry),
      geo_suspect: !isInsideJapan(entry.lat, entry.lon),
      raw: entry,
    });
  }
  return [...byId.values()];
};

const callRpc = async (env, system_id, fetched_at, rows) => {
  const response = await fetch(`${env.SUPABASE_URL}/rest/v1/rpc/upsert_station_attributes`, {
    method: "POST",
    headers: { ...authHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify({ p_system_id: system_id, p_fetched_at: fetched_at, p_rows: rows }),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`RPC が ${response.status}: ${text.slice(0, 200)}`);
  }
  return JSON.parse(text);
};

const main = async () => {
  const [envPath = ".env", objectPath] = process.argv.slice(2);
  if (objectPath === undefined) {
    throw new Error("Storage のパスを指定してください（例: hellocycling/2026/09/06/…json.gz）");
  }
  const env = readEnv(envPath);
  const system_id = objectPath.split("/")[0];
  const epoch_s = Number(objectPath.replace(/^.*station_information_(\d+)\.json\.gz$/, "$1"));
  if (!Number.isFinite(epoch_s)) {
    throw new Error(`ファイル名から epoch を読めません: ${objectPath}`);
  }

  const feed = await downloadFeed(env, objectPath);
  const rows = toRows(feed);
  const fetched_at = new Date(feed.last_updated * 1000).toISOString();

  console.log(`system      : ${system_id}`);
  console.log(`object      : ${objectPath}`);
  console.log(`last_updated: ${fetched_at}`);
  console.log(`rows        : ${feed.data.stations.length} 行 → 重複排除後 ${rows.length} ポート`);
  console.log(`geo_suspect : ${rows.filter((r) => r.geo_suspect).length} 件`);

  const result = await callRpc(env, system_id, fetched_at, rows);
  console.log(`結果        : ${JSON.stringify(result)}`);

  if (result.n_skipped_older > 0) {
    console.log("");
    console.log(
      `注意: ${result.n_skipped_older} 件が n_skipped_older。既により新しい有効行がある。` +
        "ベースラインは日次同期より先に入れること。",
    );
  }
};

await main();
