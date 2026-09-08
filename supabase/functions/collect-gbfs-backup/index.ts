/**
 * バックアップ収集器（W3 プラン §5.5、開発プラン R13・R18）。
 *
 * **やることは 3 つだけ**（W3-03）。
 *   1. ODPT の**公開**エンドポイントから `station_status.json` を取る（トークン無し）
 *   2. `last_updated` を読み、gzip して `gbfs-raw` の**同じパス**に `upsert: false` で置く
 *   3. `job_runs` に記録する
 *
 * **やらないこと**：正規化、`ingest_snapshot` の呼び出し、`feed_state` の更新。
 * 守るべき不変条件は「生 JSON を必ず残す」（CLAUDE.md 最重要原則 1）で、Postgres の
 * 配列は生 JSON から作り直せる（W2 の PR B で実証、照合 6/6 一致）。RPC を呼ぶには
 * 正規化（負値の丸め・重複排除・フラグ・`idx` の採番）を Deno で再現することになり、
 * **本番と冗長系で正規化がずれる**という新しいリスクを作る。
 *
 * **これはデータの保全だけを担う。** Vercel が落ちているとき `/v1` も推論も一緒に
 * 止まっている。復旧後は `scripts/rebuild-snapshots.ts` で DB に戻す（§9.2）。
 *
 * 認証は `Authorization: Bearer <CRON_SECRET>`。Vercel の Cron ハンドラと同じ規律で、
 * `verify_jwt` は切ってある（`supabase/config.toml`）。JWT で守ると呼ぶ側が
 * サービスロールキーを持つ必要があり、**最も強い鍵を Vault に置くことになる**。
 * 既に同じ用途で使っている `CRON_SECRET` のほうが影響範囲が小さい。
 */
import {
  BackupError,
  FEED_NAME,
  GZIP_CONTENT_TYPE,
  RAW_BUCKET,
  isAuthorized,
  isDuplicateUpload,
  isKnownSystem,
  rawObjectPath,
  readLastUpdated,
  rpcUrl,
  statusFeedUrl,
  storageObjectUrl,
} from "./core.ts";

/** ODPT への 1 回の取得の上限。Edge Function の実時間には余裕がある。 */
const FETCH_TIMEOUT_MS = 20_000;

/** ODPT から見て誰の取得かが分かるようにする（開発プラン §5.1 の 18）。 */
const USER_AGENT = "BikeChance/0.1 (backup collector)";

type Env = {
  readonly supabase_url: string;
  readonly service_key: string;
  readonly cron_secret: string | undefined;
};

const readEnv = (): Env => {
  // Supabase が自動で注入する 2 つ。**新しい秘密はここに増やさない**（W3-04）
  const supabase_url = Deno.env.get("SUPABASE_URL") ?? "";
  const service_key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
  if (supabase_url === "" || service_key === "") {
    // **値は出さない。** 足りないときに出すのは変数名だけ（CLAUDE.md §5）
    throw new BackupError({
      phase: "config",
      http_status: null,
      message: "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY が未設定",
    });
  }
  return { supabase_url, service_key, cron_secret: Deno.env.get("CRON_SECRET") };
};

const serviceHeaders = (env: Env): Record<string, string> => ({
  apikey: env.service_key,
  Authorization: `Bearer ${env.service_key}`,
});

/** ODPT から生のバイト列を取る。**パースも再直列化もしない**（保存するのは原文）。 */
const fetchFeed = async (system_id: string): Promise<Uint8Array> => {
  if (!isKnownSystem(system_id)) {
    throw new BackupError({ phase: "validate", http_status: null, message: "未知のシステム" });
  }
  const received = await fetch(statusFeedUrl(system_id), {
    headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    redirect: "error",
  }).catch((cause: unknown) => {
    throw new BackupError({
      phase: "fetch",
      http_status: null,
      message: cause instanceof Error ? cause.name : "FetchFailed",
    });
  });
  if (received.status !== 200) {
    throw new BackupError({
      phase: "fetch",
      http_status: received.status,
      message: "ODPT が想定外のステータスを返した",
    });
  }
  return new Uint8Array(await received.arrayBuffer());
};

/** 受信したバイト列そのものを gzip する。実測 4 MiB で約 70 ms（CPU 上限 2 秒）。 */
const gzipBytes = async (body: Uint8Array): Promise<Uint8Array> => {
  const stream = new Blob([body]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
};

/**
 * Storage に置く。**重複は正常系**（同じ観測が既にある）。
 *
 * 重複の判定は HTTP ステータスではなく**本文**で行う。Storage の REST API は
 * 重複を **400** で返し、409 は本文の `statusCode` に文字列で入る（`isDuplicateUpload`）。
 */
const putObject = async (env: Env, path: string, gzipped: Uint8Array): Promise<boolean> => {
  const received = await fetch(storageObjectUrl(env.supabase_url, RAW_BUCKET, path), {
    method: "POST",
    headers: {
      ...serviceHeaders(env),
      "Content-Type": GZIP_CONTENT_TYPE,
      "x-upsert": "false",
    },
    body: gzipped,
  });
  if (received.ok) {
    return false;
  }
  const body: unknown = await received.json().catch(() => null);
  if (isDuplicateUpload(received.status, body)) {
    return true;
  }
  throw new BackupError({
    phase: "storage",
    http_status: received.status,
    message: "Storage が想定外のステータスを返した",
  });
};

const callRpc = async (env: Env, name: string, body: unknown): Promise<unknown> => {
  const received = await fetch(rpcUrl(env.supabase_url, name), {
    method: "POST",
    headers: { ...serviceHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!received.ok) {
    throw new BackupError({
      phase: "rest",
      http_status: received.status,
      message: `${name} が失敗`,
    });
  }
  return received.json();
};

/** 記録できなくても保存は続ける。**保存できたことのほうが大事**（`compact` と同じ規律）。 */
const jobStarted = async (env: Env, job_name: string): Promise<number | null> => {
  const value = await callRpc(env, "job_started", { p_job_name: job_name }).catch(() => null);
  return typeof value === "number" ? value : null;
};

const jobFinished = async (
  env: Env,
  run_id: number | null,
  status: string,
  detail: Record<string, unknown>,
): Promise<void> => {
  if (run_id === null) return;
  await callRpc(env, "job_finished", { p_id: run_id, p_status: status, p_detail: detail }).catch(
    (cause: unknown) => {
      console.error(`job_finished に失敗した: ${cause instanceof Error ? cause.name : "unknown"}`);
    },
  );
};

/** 失敗を `{ phase, http_status, error_name }` に詰め替える。素の例外を外に出さない。 */
const toFailure = (cause: unknown): Record<string, unknown> =>
  cause instanceof BackupError
    ? { phase: cause.phase, http_status: cause.http_status, error_name: cause.message }
    : {
        phase: "unknown",
        http_status: null,
        error_name: cause instanceof Error ? cause.name : "unknown",
      };

const collect = async (env: Env, system_id: string): Promise<Record<string, unknown>> => {
  const started = Date.now();
  const run_id = await jobStarted(env, `backup_collect:${system_id}`);
  try {
    const body = await fetchFeed(system_id);
    const epoch_s = readLastUpdated(new TextDecoder().decode(body));
    const path = rawObjectPath({ system_id, feed: FEED_NAME, epoch_s });
    const gzipped = await gzipBytes(body);
    const duplicate = await putObject(env, path, gzipped);
    const detail = {
      system_id,
      observed_at: new Date(epoch_s * 1000).toISOString(),
      path,
      bytes: body.byteLength,
      gzip_bytes: gzipped.byteLength,
      duplicate,
      duration_ms: Date.now() - started,
    };
    await jobFinished(env, run_id, "ok", detail);
    return { ok: true, ...detail };
  } catch (cause) {
    const detail = { system_id, ...toFailure(cause), duration_ms: Date.now() - started };
    await jobFinished(env, run_id, "failed", detail);
    return { ok: false, ...detail };
  }
};

const json = (body: unknown, status: number): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });

Deno.serve(async (request: Request): Promise<Response> => {
  let env: Env;
  try {
    env = readEnv();
  } catch (cause) {
    return json(toFailure(cause), 500);
  }

  // 認証。**DB にもログにも何も書かずに弾く**（Vercel の Cron ハンドラと同じ順序）
  if (!(await isAuthorized(request.headers.get("authorization"), env.cron_secret))) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }

  const system_id = new URL(request.url).searchParams.get("system") ?? "";
  if (!isKnownSystem(system_id)) {
    return json({ ok: false, error: "unknown_system" }, 400);
  }

  const outcome = await collect(env, system_id);
  return json(outcome, outcome["ok"] === true ? 200 : 500);
});
