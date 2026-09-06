/**
 * Storage の生 JSON と DB のスナップショットを UTC 日で突き合わせる（W1 プラン §8.3）。
 *
 * **PR 0 の期間は差分が出るのが正常。** 段 0 から PR D のマージまで、生 JSON は貯まるが
 * `status_snapshots` には行が無い。この照合は **PR D が終日稼働した UTC 日**にだけ
 * 合格を求める。PR D をまたぐ日は、マージ時刻より後の分だけを見る。
 *
 * 使い方:
 *   node scripts/reconcile-raw.mjs <環境ファイル> [UTC の日付] [この時刻以降だけ数える(ISO)]
 *   node scripts/reconcile-raw.mjs .env 2026-09-06
 *   node scripts/reconcile-raw.mjs .env 2026-09-06 2026-09-06T09:00:00Z
 */
import { readFileSync } from "node:fs";

const SYSTEMS = ["hellocycling", "docomo-cycle"];

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

/** Storage 上の station_status オブジェクトの epoch 一覧（1,000 件ずつ辿る）。 */
const listStorageEpochs = async (env, system, datePath) => {
  const epochs = new Set();
  const limit = 1000;
  for (let offset = 0; ; offset += limit) {
    const response = await fetch(`${env.SUPABASE_URL}/storage/v1/object/list/gbfs-raw`, {
      method: "POST",
      headers: { ...authHeaders(env), "Content-Type": "application/json" },
      body: JSON.stringify({ prefix: `${system}/${datePath}/`, limit, offset }),
    });
    if (!response.ok) {
      throw new Error(`Storage list が ${response.status}`);
    }
    const page = await response.json();
    for (const object of page) {
      if (object.name?.startsWith("station_status_")) {
        epochs.add(Number(object.name.replace("station_status_", "").replace(".json.gz", "")));
      }
    }
    if (page.length < limit) return epochs;
  }
};

/** DB のスナップショットの epoch 一覧。 */
// PostgREST の max_rows は 1000 で、超えた分は警告なく切り詰められる（§4.3 の 11）。
// ドコモは 1 日 1,080 件になるため、ページングが無いと差分を見誤る
const PAGE_SIZE = 1000;

const listDbEpochs = async (env, system, dayStart, dayEnd) => {
  const epochs = new Set();
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const response = await fetch(
      `${env.SUPABASE_URL}/rest/v1/status_snapshots?select=observed_at` +
        `&system_id=eq.${system}&observed_at=gte.${dayStart}&observed_at=lt.${dayEnd}` +
        `&order=observed_at.asc&limit=${PAGE_SIZE}&offset=${offset}`,
      { headers: authHeaders(env) },
    );
    if (!response.ok) {
      throw new Error(`REST status_snapshots が ${response.status}`);
    }
    const rows = await response.json();
    for (const row of rows) epochs.add(Math.floor(Date.parse(row.observed_at) / 1000));
    if (rows.length < PAGE_SIZE) return epochs;
  }
};

const formatEpoch = (epoch) => new Date(epoch * 1000).toISOString().slice(11, 19);

const main = async () => {
  const [envPath, day = new Date().toISOString().slice(0, 10), since] = process.argv.slice(2);
  if (!envPath) {
    console.error("使い方: node scripts/reconcile-raw.mjs <環境ファイル> [UTC の日付] [開始時刻]");
    process.exit(1);
  }
  const env = readEnv(envPath);
  const datePath = day.replaceAll("-", "/");
  const sinceEpoch = since ? Math.floor(Date.parse(since) / 1000) : 0;

  console.log(`UTC ${day}${since ? `（${since} 以降だけ数える）` : ""}\n`);
  let failed = false;

  for (const system of SYSTEMS) {
    const storage = [...(await listStorageEpochs(env, system, datePath))].filter(
      (e) => e >= sinceEpoch,
    );
    const db = [
      ...(await listDbEpochs(env, system, `${day}T00:00:00Z`, `${day}T23:59:59.999Z`)),
    ].filter((e) => e >= sinceEpoch);
    const storageSet = new Set(storage);
    const dbSet = new Set(db);
    const onlyStorage = storage.filter((e) => !dbSet.has(e)).sort();
    const onlyDb = db.filter((e) => !storageSet.has(e)).sort();

    console.log(`${system}`);
    console.log(
      `  Storage ${storage.length.toLocaleString()} 件 / DB ${db.length.toLocaleString()} 件`,
    );
    if (onlyStorage.length > 0) {
      console.log(
        `  Storage にしかない ${onlyStorage.length} 件: ${onlyStorage.slice(0, 5).map(formatEpoch).join(", ")}`,
      );
    }
    if (onlyDb.length > 0) {
      console.log(
        `  DB にしかない ${onlyDb.length} 件: ${onlyDb.slice(0, 5).map(formatEpoch).join(", ")}`,
      );
    }
    if (onlyStorage.length === 0 && onlyDb.length === 0) {
      console.log(`  差分 0`);
    } else {
      failed = true;
    }
  }

  console.log(
    failed
      ? "\n差分あり。PR D 稼働前の生 JSON が混ざっていないか、開始時刻を指定して確かめてください"
      : "\n差分 0（§8.3 合格）",
  );
  process.exit(failed ? 1 : 0);
};

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
