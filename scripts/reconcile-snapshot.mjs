/**
 * 生 JSON 1 件と DB の配列を `idx` で突き合わせる（W1 プラン §8.6）。
 *
 * **これが W1 で最も重要な検証**である。配列の並びと `idx` の対応は、型でも制約でも
 * 守れない。ここが狂うと、あるポートの台数が別のポートの行に入ったまま何か月も
 * 気づかず、学習データが静かに汚染される。§8.6 が通らなければ PR E1 に進まない。
 *
 * **正規化はここで独立に書き直している。** `gbfs-core` を使うと「DB が gbfs-core の
 * 出力と一致する」ことしか言えない。別々に書いた実装が同じ答えに至って初めて、
 * 規則そのものが正しいと言える（ゴールデンテストと同じ考え方）。
 *
 * 使い方:
 *   node scripts/reconcile-snapshot.mjs <環境ファイル> <system_id> [観測時刻(ISO)]
 *   node scripts/reconcile-snapshot.mjs .env hellocycling
 *   node scripts/reconcile-snapshot.mjs .env docomo-cycle 2026-09-06T09:15:00Z
 *
 * 観測時刻を省略すると、最新のスナップショットを照合する。
 */
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

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

const rest = async (env, path) => {
  const response = await fetch(`${env.SUPABASE_URL}/rest/v1/${path}`, {
    headers: {
      apikey: env.SUPABASE_SECRET_KEY,
      Authorization: `Bearer ${env.SUPABASE_SECRET_KEY}`,
    },
  });
  if (!response.ok) {
    throw new Error(`REST ${path} が ${response.status}: ${(await response.text()).slice(0, 200)}`);
  }
  return response.json();
};

/**
 * PostgREST の `max_rows` は 1000 で、**超えた分は警告なく切り詰められる**
 * （W1 プラン §4.3 の 11）。`limit=100000` と書いても効かない。
 * 2 万件の台帳を 1000 件と誤認すると、照合が「全部一致」に見えてしまう。
 */
const PAGE_SIZE = 1000;

const restAll = async (env, path) => {
  const rows = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const page = await rest(env, `${path}&limit=${PAGE_SIZE}&offset=${offset}`);
    rows.push(...page);
    if (page.length < PAGE_SIZE) return rows;
  }
};

const downloadRaw = async (env, path) => {
  const response = await fetch(`${env.SUPABASE_URL}/storage/v1/object/gbfs-raw/${path}`, {
    headers: {
      apikey: env.SUPABASE_SECRET_KEY,
      Authorization: `Bearer ${env.SUPABASE_SECRET_KEY}`,
    },
  });
  if (!response.ok) {
    throw new Error(`Storage ${path} が ${response.status}`);
  }
  return JSON.parse(gunzipSync(Buffer.from(await response.arrayBuffer())).toString("utf8"));
};

/** gbfs-core とは独立に書いた正規化。規則は §11.1 に従う。 */
const normalize = (doc) => {
  const observed = doc.last_updated;
  const byId = new Map();
  for (const s of doc.data.stations) {
    if (byId.has(s.station_id)) continue; // 重複は先頭を残す
    const reported = s.last_reported ?? observed;
    byId.set(s.station_id, {
      bikes: s.num_bikes_available < 0 ? 0 : s.num_bikes_available,
      docks: s.num_docks_available < 0 ? 0 : s.num_docks_available,
      flags: (s.is_installed ? 1 : 0) + (s.is_renting ? 2 : 0) + (s.is_returning ? 4 : 0),
      reported_age_s: Math.min(Math.max(observed - reported, 0), 32767),
    });
  }
  return byId;
};

const MISSING = -1;

/**
 * 配列の位置 idx+1 と、生 JSON から作った期待値を 1 ポートずつ比べる。
 *
 * **台帳の全件とは比べない。** 配列の長さは「そのスナップショットを取り込んだ時点で
 * 登録済みだったポート数」で、過去の行は当時の長さのままである（§11.1）。ドコモは
 * ポート ID が出入りするため台帳は少しずつ伸びる。古いスナップショットを今の台帳と
 * 比べると、後から登録されたポートの分だけ「不一致」に見えてしまう。
 * 比較の対象は `idx < 配列長` のポートだけ。
 */
const compare = (stations, snapshot, expected) => {
  const mismatches = [];
  let present = 0;
  let missing = 0;
  let registeredLater = 0;

  for (const { station_id, idx } of stations) {
    if (idx >= snapshot.bikes.length) {
      registeredLater += 1;
      continue;
    }
    const want = expected.get(station_id);
    const got = {
      bikes: snapshot.bikes[idx],
      docks: snapshot.docks[idx],
      flags: snapshot.flags[idx],
      reported_age_s: snapshot.reported_age_s[idx],
    };
    if (want === undefined) {
      missing += 1;
      const allMissing = Object.values(got).every((v) => v === MISSING);
      if (!allMissing) {
        mismatches.push({ station_id, idx, want: "すべて -1（不在）", got });
      }
      continue;
    }
    present += 1;
    for (const key of ["bikes", "docks", "flags", "reported_age_s"]) {
      if (got[key] !== want[key]) {
        mismatches.push({ station_id, idx, key, want: want[key], got: got[key] });
      }
    }
  }
  return { mismatches, present, missing, registeredLater };
};

const main = async () => {
  const [envPath, systemId, observedAt] = process.argv.slice(2);
  if (!envPath || !systemId) {
    console.error(
      "使い方: node scripts/reconcile-snapshot.mjs <環境ファイル> <system_id> [観測時刻]",
    );
    process.exit(1);
  }
  const env = readEnv(envPath);

  const filter = observedAt ? `&observed_at=eq.${encodeURIComponent(observedAt)}` : "";
  const rows = await rest(
    env,
    `status_snapshots?select=observed_at,n_stations,is_anomalous,raw_path,bikes,docks,flags,reported_age_s` +
      `&system_id=eq.${systemId}${filter}&order=observed_at.desc&limit=1`,
  );
  if (rows.length === 0) {
    console.error(`${systemId} のスナップショットが見つかりません`);
    process.exit(1);
  }
  const snapshot = rows[0];

  const stations = await restAll(
    env,
    `stations?select=station_id,idx&system_id=eq.${systemId}&order=idx.asc`,
  );
  const raw = await downloadRaw(env, snapshot.raw_path);
  const expected = normalize(raw);

  console.log(`system      : ${systemId}`);
  console.log(`observed_at : ${snapshot.observed_at}`);
  console.log(`raw_path    : ${snapshot.raw_path}`);
  console.log(
    `生 JSON     : ${raw.data.stations.length.toLocaleString()} 行 → 重複排除後 ${expected.size.toLocaleString()} ポート`,
  );
  console.log(
    `DB          : 配列長 ${snapshot.bikes.length.toLocaleString()} / n_stations ${snapshot.n_stations.toLocaleString()} / 台帳 ${stations.length.toLocaleString()} ポート`,
  );

  const problems = [];
  if (raw.last_updated * 1000 !== Date.parse(snapshot.observed_at)) {
    problems.push(`observed_at が生 JSON の last_updated と一致しない`);
  }
  // 配列は台帳を超えない。等しくないのは「その後にポートが登録された」だけで正常
  if (snapshot.bikes.length > stations.length) {
    problems.push(`配列長 ${snapshot.bikes.length} が台帳の ${stations.length} を超えている`);
  }
  if (snapshot.n_stations !== expected.size) {
    problems.push(`n_stations ${snapshot.n_stations} が重複排除後の ${expected.size} と一致しない`);
  }
  if (snapshot.bikes.length < snapshot.n_stations) {
    problems.push(
      `配列長 ${snapshot.bikes.length} が n_stations ${snapshot.n_stations} を下回っている`,
    );
  }
  for (const key of ["docks", "flags", "reported_age_s"]) {
    if (snapshot[key].length !== snapshot.bikes.length) {
      problems.push(`${key} の長さが bikes と揃っていない`);
    }
  }

  const { mismatches, present, missing } = compare(stations, snapshot, expected);
  console.log(
    `照合        : 一致 ${(present + missing - mismatches.length).toLocaleString()} / 現れた ${present.toLocaleString()} / 不在 ${missing.toLocaleString()}`,
  );

  if (problems.length > 0) {
    console.error("\n構造の不一致:");
    for (const p of problems) console.error(`  - ${p}`);
  }
  if (mismatches.length > 0) {
    console.error(`\n値の不一致 ${mismatches.length} 件（先頭 10 件）:`);
    for (const m of mismatches.slice(0, 10)) console.error(`  ${JSON.stringify(m)}`);
  }
  if (problems.length === 0 && mismatches.length === 0) {
    console.log("\n不一致 0。配列の並びと idx の対応が保たれている（§8.6 合格）");
    process.exit(0);
  }
  process.exit(1);
};

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
