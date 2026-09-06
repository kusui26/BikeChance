/**
 * `ingest_snapshot` の所要時間を実データで測る（W1 プラン §6.5 の完了条件）。
 *
 * 目標：**初回（全件新規）1 秒以内、定常 0.5 秒以内**。
 * プロトタイプの実測は 0.65 秒 / 0.45 秒（W1 プラン §4.1 (b)）。
 *
 * **PostgREST 経由で測る。** 本番の収集器は supabase-js の RPC（= PostgREST）で呼ぶため
 * （W1-1）、直接接続で測ると HTTP とゲートウェイの往復が抜け、実際より速く見える。
 *
 * 正規化はここで最小限に書き直している。`packages/gbfs-core` が本物で、こちらは
 * ペイロードの形と大きさを実物に合わせるためのもの。**規則が一致していることは
 * gbfs-core のゴールデンテストが担保している**ので、ここでの目的は所要時間だけ。
 *
 * 使い方:
 *   node scripts/bench-ingest.mjs [環境ファイル] [フィクスチャの日付]
 *   node scripts/bench-ingest.mjs apps/web/.env.local 2026-09-06
 */
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

const SYSTEMS = ["hellocycling", "docomo-cycle"];
const STEADY_RUNS = 3;
const FIRST_LOAD_BUDGET_MS = 1000;
const STEADY_BUDGET_MS = 500;

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

const readFixture = (date, system, feed) =>
  JSON.parse(
    gunzipSync(readFileSync(`fixtures/gbfs/${date}/${system}.${feed}.json.gz`)).toString("utf8"),
  );

const toFlags = (s) => (s.is_installed ? 1 : 0) + (s.is_renting ? 2 : 0) + (s.is_returning ? 4 : 0);
const clamp = (v) => (v < 0 ? 0 : v);

/** 重複は先頭を残し、負値は 0 に丸める（gbfs-core と同じ規則）。 */
const normalize = (doc) => {
  const observed = doc.last_updated;
  const byId = new Map();
  for (const s of doc.data.stations) {
    if (byId.has(s.station_id)) continue;
    byId.set(s.station_id, {
      station_id: s.station_id,
      bikes: clamp(s.num_bikes_available),
      docks: clamp(s.num_docks_available),
      flags: toFlags(s),
      reported_age_s: Math.min(Math.max(observed - (s.last_reported ?? observed), 0), 32767),
    });
  }
  return { observed, stations: [...byId.values()] };
};

const buildArgs = (system, { observed, stations }, offsetSeconds, jitter) => ({
  p_system_id: system,
  p_observed_at: new Date((observed + offsetSeconds) * 1000).toISOString(),
  p_fetched_at: new Date().toISOString(),
  p_etag: `bench-${offsetSeconds}`,
  p_station_ids: stations.map((s) => s.station_id),
  // 定常の測定では一部の台数を動かし、n_changed が現実的な値になるようにする
  p_bikes: stations.map((s, i) => (jitter && i % 40 === 0 ? (s.bikes + 1) % 50 : s.bikes)),
  p_docks: stations.map((s) => s.docks),
  p_flags: stations.map((s) => s.flags),
  p_reported_age_s: stations.map((s) => s.reported_age_s),
  p_raw_path: `bench/${system}/${offsetSeconds}`,
});

const callRpc = async (env, name, args) => {
  const started = performance.now();
  const response = await fetch(`${env.SUPABASE_URL}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: {
      apikey: env.SUPABASE_SECRET_KEY,
      Authorization: `Bearer ${env.SUPABASE_SECRET_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(args),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`${name} が ${response.status} を返した: ${text.slice(0, 300)}`);
  }
  return { elapsed_ms: performance.now() - started, result: JSON.parse(text) };
};

const format = (label, elapsed, budget, result) => {
  const verdict = elapsed < budget ? "OK " : "超過";
  const detail = [
    `n_stations=${result.n_stations ?? "-"}`,
    `n_new=${result.n_new_stations ?? "-"}`,
    `n_changed=${result.n_changed ?? "-"}`,
    `len=${result.array_length ?? "-"}`,
  ].join(" ");
  return `  ${verdict} ${label.padEnd(16)} ${elapsed.toFixed(0).padStart(5)} ms（上限 ${budget}）  ${detail}`;
};

const main = async () => {
  const [envPath = "apps/web/.env.local", date = "2026-09-06"] = process.argv.slice(2);
  const env = readEnv(envPath);
  if (!env.SUPABASE_URL || !env.SUPABASE_SECRET_KEY) {
    console.error(`${envPath} に SUPABASE_URL / SUPABASE_SECRET_KEY がありません`);
    process.exit(1);
  }

  let failed = false;
  for (const system of SYSTEMS) {
    const feed = normalize(readFixture(date, system, "station_status"));
    console.log(`\n${system}（${feed.stations.length.toLocaleString()} ポート）`);

    const first = await callRpc(env, "ingest_snapshot", buildArgs(system, feed, 0, false));
    console.log(format("初回（全件新規）", first.elapsed_ms, FIRST_LOAD_BUDGET_MS, first.result));
    failed ||= first.elapsed_ms >= FIRST_LOAD_BUDGET_MS;

    const steady = [];
    for (let run = 1; run <= STEADY_RUNS; run += 1) {
      const call = await callRpc(env, "ingest_snapshot", buildArgs(system, feed, run * 300, true));
      steady.push(call);
      console.log(format(`定常 ${run} 回目`, call.elapsed_ms, STEADY_BUDGET_MS, call.result));
    }
    const median = [...steady.map((c) => c.elapsed_ms)].sort((a, b) => a - b)[
      Math.floor(steady.length / 2)
    ];
    console.log(`  定常の中央値: ${median.toFixed(0)} ms`);
    failed ||= median >= STEADY_BUDGET_MS;

    const duplicate = await callRpc(env, "ingest_snapshot", buildArgs(system, feed, 300, true));
    console.log(format("二重配信", duplicate.elapsed_ms, STEADY_BUDGET_MS, duplicate.result));
  }

  console.log(failed ? "\n目標を超えた測定があります" : "\nすべて目標内");
  process.exit(failed ? 1 : 0);
};

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
