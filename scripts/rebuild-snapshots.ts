/**
 * 生 JSON から `status_snapshots` を作り直す（W2 プラン §5.4、PR B）。
 *
 * CLAUDE.md §6 は「生 JSON からの再構築スクリプトを常に動く状態に保つ」と定めている。
 * 60 日保持の削除で 2026-11-30 に 9 月分のパーティションが落ちるため、それまでに
 * 「生 JSON → Postgres」の経路が動くことを実データで確かめておく必要がある。
 *
 * **正規化は `packages/gbfs-core` をそのまま使う**（W2-05）。負値の丸め・重複排除・
 * フラグ・経過秒の規則を書き直すと、本番の収集と再構築で結果がずれる。再構築は
 * 「本番と同じ手続きをやり直す」ことなので、同じ実装を通す。
 *
 * **`ingest_snapshot` だけを呼ぶ**（W2-07）。`begin_fetch` / `finish_fetch` は
 * `feed_state`（claim・ETag・連続失敗・最終観測）を書くので、過去のデータを流し込む
 * ときに触ると現在の収集の状態が壊れる。
 *
 * **必ず古い順に流す**（W2-08）。`stations.idx` は「その時点で未登録のポートを入力順に
 * 末尾へ足す」で採番されるため、順序が違えば別の値になる。
 *
 * 使い方:
 *   pnpm exec tsx scripts/rebuild-snapshots.ts <環境ファイル> <system> <開始日> <終了日> [--dry-run]
 *   pnpm exec tsx scripts/rebuild-snapshots.ts .env hellocycling 2026-09-06 2026-09-06 --dry-run
 *
 * 日付は **UTC**（Storage のパスと同じ）。開始日・終了日とも含む。
 */
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import {
  buildIngestArgs,
  normalizeStationStatus,
  parseStationStatusFeed,
  type IngestArgs,
} from "@bikechance/gbfs-core";
import {
  RAW_BUCKET,
  SYSTEM_IDS,
  parseRawObjectName,
  utcDayPathsBetween,
  type SystemId,
} from "@bikechance/shared";

const PAGE_SIZE = 1000;
const MS_PER_S = 1000;
const DAY_MS = 86_400_000;

type Env = Readonly<Record<string, string>>;

/** Storage 上の 1 オブジェクト。再構築に要る最小限だけ持つ。 */
type RawObject = {
  readonly path: string;
  /** ファイル名から読んだフィードの `last_updated`。並べ替えのキーでもある。 */
  readonly epoch_s: number;
  /** Storage に保存された時刻。応答の受信時刻は復元できないのでこれを使う。 */
  readonly created_at: string;
};

/** Storage の list が返す 1 要素のうち、使う項目だけを見る型ガード。 */
const isListEntry = (value: unknown): value is { name?: string; created_at?: string } =>
  typeof value === "object" && value !== null;

const isObservedAt = (value: unknown): value is { observed_at: string } =>
  typeof value === "object" &&
  value !== null &&
  typeof Reflect.get(value, "observed_at") === "string";

type Outcome = {
  readonly inserted: number;
  readonly duplicate: number;
  readonly locked: number;
  readonly failed: readonly string[];
};

const readEnv = (path: string): Env =>
  Object.fromEntries(
    readFileSync(path, "utf8")
      .split("\n")
      .filter((line) => line.includes("=") && !line.trimStart().startsWith("#"))
      .map((line) => {
        const at = line.indexOf("=");
        return [line.slice(0, at).trim(), line.slice(at + 1).trim()];
      }),
  );

const authHeaders = (env: Env): Record<string, string> => ({
  apikey: env["SUPABASE_SECRET_KEY"] ?? "",
  Authorization: `Bearer ${env["SUPABASE_SECRET_KEY"] ?? ""}`,
});

const isSystemId = (value: string): value is SystemId =>
  SYSTEM_IDS.some((system_id) => system_id === value);

/** `YYYY-MM-DD` を UTC の日付として検証する。 */
const parseUtcDate = (value: string, label: string): Date => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error(`${label} は YYYY-MM-DD で指定してください: ${value}`);
  }
  const at = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(at.getTime())) {
    throw new Error(`${label} が日付として読めません: ${value}`);
  }
  return at;
};

const listDay = async (env: Env, system_id: SystemId, dayPath: string): Promise<RawObject[]> => {
  const found: RawObject[] = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const response = await fetch(`${env["SUPABASE_URL"]}/storage/v1/object/list/${RAW_BUCKET}`, {
      method: "POST",
      headers: { ...authHeaders(env), "Content-Type": "application/json" },
      body: JSON.stringify({ prefix: `${system_id}/${dayPath}/`, limit: PAGE_SIZE, offset }),
    });
    if (!response.ok) {
      throw new Error(`Storage list が ${response.status}（${system_id}/${dayPath}）`);
    }
    const body: unknown = await response.json();
    if (!Array.isArray(body)) {
      throw new Error(`Storage list が配列を返さなかった（${system_id}/${dayPath}）`);
    }
    const page: readonly unknown[] = body;
    for (const entry of page) {
      const object = isListEntry(entry) ? entry : {};
      const name = object.name ?? "";
      // パス規約の読み取りは shared に置いてある（書く側と同じファイル）
      const parsed = parseRawObjectName(name);
      if (parsed !== null && parsed.feed === "station_status") {
        found.push({
          path: `${system_id}/${dayPath}/${name}`,
          epoch_s: parsed.epoch_s,
          created_at: object.created_at ?? new Date(parsed.epoch_s * MS_PER_S).toISOString(),
        });
      }
    }
    if (page.length < PAGE_SIZE) {
      return found;
    }
  }
};

/**
 * 既に取り込み済みの `observed_at`（POSIX 秒）を集める。
 *
 * `ingest_snapshot` は重複を `duplicate` として弾くので、これが無くても結果は同じ。
 * **効かせたいのはダウンロードの節約**で、ドコモの 1 日は 1,000 件を超える。全部落として
 * から捨てるのは帯域の無駄が大きい。
 *
 * PostgREST の `max_rows` は 1000 で、超えた分は警告なく切り詰められる
 * （W1 プラン §4.3 の 11）。必ずページングする。
 */
const listIngestedEpochs = async (
  env: Env,
  system_id: SystemId,
  from: Date,
  to: Date,
): Promise<ReadonlySet<number>> => {
  const epochs = new Set<number>();
  const fromIso = from.toISOString();
  // 終了日を含めるため 24 時間後まで
  const toIso = new Date(to.getTime() + DAY_MS).toISOString();
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const query =
      `select=observed_at&system_id=eq.${system_id}` +
      `&observed_at=gte.${fromIso}&observed_at=lt.${toIso}` +
      `&order=observed_at.asc&limit=${PAGE_SIZE}&offset=${offset}`;
    const response = await fetch(`${env["SUPABASE_URL"]}/rest/v1/status_snapshots?${query}`, {
      headers: authHeaders(env),
    });
    if (!response.ok) {
      throw new Error(`REST status_snapshots が ${response.status}`);
    }
    const body: unknown = await response.json();
    if (!Array.isArray(body)) {
      throw new Error("status_snapshots が配列を返さなかった");
    }
    const rows: readonly unknown[] = body;
    for (const row of rows) {
      const at = isObservedAt(row) ? Date.parse(row.observed_at) : Number.NaN;
      if (!Number.isNaN(at)) {
        epochs.add(Math.floor(at / MS_PER_S));
      }
    }
    if (rows.length < PAGE_SIZE) {
      return epochs;
    }
  }
};

const downloadFeed = async (env: Env, path: string): Promise<unknown> => {
  const response = await fetch(`${env["SUPABASE_URL"]}/storage/v1/object/${RAW_BUCKET}/${path}`, {
    headers: authHeaders(env),
  });
  if (!response.ok) {
    throw new Error(`Storage ${path} が ${response.status}`);
  }
  const gz = Buffer.from(await response.arrayBuffer());
  return JSON.parse(gunzipSync(gz).toString("utf8"));
};

const callRpc = async (env: Env, name: string, body: unknown): Promise<unknown> => {
  const response = await fetch(`${env["SUPABASE_URL"]}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: { ...authHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`RPC ${name} が ${response.status}: ${text.slice(0, 200)}`);
  }
  return text === "" ? null : JSON.parse(text);
};

/**
 * 対象月のパーティションが在るか確かめる。**無ければ止まる。**
 * 無いまま流すと行は DEFAULT パーティションに落ち、そこは「保守が止まっている印」
 * として監視が通知する場所であってデータの置き場所ではない（W1-15、§5.4）。
 */
const assertPartitions = async (env: Env, objects: readonly RawObject[]): Promise<void> => {
  const months = new Set(objects.map((o) => o.path.split("/").slice(1, 3).join("/")));
  for (const month of months) {
    const [year, mm] = month.split("/");
    const at = `${year}-${mm}-01T00:00:00Z`;
    const exists = await callRpc(env, "snapshot_partition_exists", { p_at: at });
    if (exists !== true) {
      throw new Error(
        `${year}-${mm} のパーティションがありません。DEFAULT に落ちるので中止します。` +
          `作成してから流し直してください（W2 プラン §4 の 3）`,
      );
    }
  }
};

const toStatus = (value: unknown): string => {
  if (typeof value === "object" && value !== null && "status" in value) {
    const status = Reflect.get(value, "status");
    return typeof status === "string" ? status : "unknown";
  }
  return "unknown";
};

const ingestOne = async (
  env: Env,
  system_id: SystemId,
  object: RawObject,
  minPresenceRatio: number | null,
): Promise<string> => {
  const document = await downloadFeed(env, object.path);
  const parsed = parseStationStatusFeed(document);
  if (!parsed.ok) {
    throw new Error(`検証に失敗: ${parsed.issues.join(" / ")}`);
  }
  const args: IngestArgs = buildIngestArgs({
    system_id,
    feed: normalizeStationStatus(parsed.feed),
    fetched_at: new Date(object.created_at),
    // 応答の ETag は生 JSON に残らない。取得そのものの記録であってスナップショットの
    // 内容ではないので、再構築の対象にしない
    etag: null,
    raw_path: object.path,
  });
  const body =
    minPresenceRatio === null ? args : { ...args, p_min_presence_ratio: minPresenceRatio };
  return toStatus(await callRpc(env, "ingest_snapshot", body));
};

const USAGE =
  "使い方: pnpm exec tsx scripts/rebuild-snapshots.ts <環境ファイル> <system> <開始日 UTC> <終了日 UTC> [--dry-run] [--force] [--min-presence-ratio=N]";

type Options = {
  readonly envPath: string;
  readonly system_id: SystemId;
  readonly fromArg: string;
  readonly toArg: string;
  readonly dryRun: boolean;
  /**
   * `ingest_snapshot` の異常ガードの閾値。既定（0.5）は「今の台帳に対して半分未満しか
   * 現れなかったら異常」という意味なので、**台帳が大きく育ったあとに古いデータを
   * 流すと、正常なスナップショットが異常と印されうる**。そのときだけ下げる。
   */
  readonly minPresenceRatio: number | null;
  /** 既に取り込み済みのものも落とし直す。既定は落とさない（ダウンロードの節約）。 */
  readonly force: boolean;
};

/** 引数を読む。足りなければ例外にする（呼び出し側で使い方を出す）。 */
export const parseOptions = (argv: readonly string[]): Options => {
  const flags = argv.filter((a) => a.startsWith("--"));
  const [envPath, system_id, fromArg, toArg] = argv.filter((a) => !a.startsWith("--"));
  if (
    envPath === undefined ||
    system_id === undefined ||
    fromArg === undefined ||
    toArg === undefined
  ) {
    throw new Error(USAGE);
  }
  if (!isSystemId(system_id)) {
    throw new Error(`未知のシステムです: ${system_id}`);
  }
  const ratioFlag = flags.find((f) => f.startsWith("--min-presence-ratio="));
  const minPresenceRatio = ratioFlag === undefined ? null : Number(ratioFlag.split("=")[1]);
  if (minPresenceRatio !== null && !Number.isFinite(minPresenceRatio)) {
    throw new Error(`--min-presence-ratio が数値ではありません: ${ratioFlag}`);
  }
  return {
    envPath,
    system_id,
    fromArg,
    toArg,
    dryRun: flags.includes("--dry-run"),
    minPresenceRatio,
    force: flags.includes("--force"),
  };
};

const main = async (): Promise<void> => {
  const {
    envPath,
    system_id: system,
    fromArg,
    toArg,
    dryRun,
    minPresenceRatio,
    force,
  } = parseOptions(process.argv.slice(2));

  const env = readEnv(envPath);
  const from = parseUtcDate(fromArg, "開始日");
  const to = parseUtcDate(toArg, "終了日");

  const days = utcDayPathsBetween(from, to);
  const listed: RawObject[] = [];
  for (const day of days) {
    listed.push(...(await listDay(env, system, day)));
  }
  // **古い順に流す**（W2-08）。stations.idx の採番順を本番と揃える
  const sorted = [...listed].sort((a, b) => a.epoch_s - b.epoch_s);
  const ingested = force ? new Set<number>() : await listIngestedEpochs(env, system, from, to);
  const objects = sorted.filter((object) => !ingested.has(object.epoch_s));

  console.log(`system      : ${system}`);
  console.log(`範囲        : ${fromArg} 〜 ${toArg}（UTC、${days.length} 日）`);
  console.log(`Storage     : ${sorted.length} オブジェクト`);
  console.log(
    `取り込み済み: ${ingested.size} 件${force ? "（--force のため落とし直す）" : "（落とさない）"}`,
  );
  console.log(`対象        : ${objects.length} オブジェクト`);
  if (objects.length === 0) {
    console.log("対象がありません。");
    return;
  }
  const first = objects[0]!;
  const last = objects.at(-1)!;
  console.log(
    `最古 / 最新 : ${new Date(first.epoch_s * MS_PER_S).toISOString()} / ${new Date(last.epoch_s * MS_PER_S).toISOString()}`,
  );

  await assertPartitions(env, objects);
  console.log("パーティション: 対象月すべて存在");

  if (dryRun) {
    console.log("");
    console.log("--dry-run のため書き込みません。");
    return;
  }

  const failed: string[] = [];
  let inserted = 0;
  let duplicate = 0;
  let locked = 0;
  for (const [index, object] of objects.entries()) {
    try {
      const status = await ingestOne(env, system, object, minPresenceRatio);
      if (status === "inserted") inserted += 1;
      else if (status === "duplicate") duplicate += 1;
      else if (status === "locked") locked += 1;
      else failed.push(`${object.path}: 想定外の status ${status}`);
    } catch (cause) {
      failed.push(`${object.path}: ${cause instanceof Error ? cause.message : String(cause)}`);
    }
    if ((index + 1) % 25 === 0 || index + 1 === objects.length) {
      console.log(`  ${index + 1}/${objects.length} 済み（新規 ${inserted} / 重複 ${duplicate}）`);
    }
  }

  const outcome: Outcome = { inserted, duplicate, locked, failed };
  console.log("");
  console.log(
    `結果        : 新規 ${outcome.inserted} / 重複 ${outcome.duplicate} / ロック ${outcome.locked} / 失敗 ${outcome.failed.length}`,
  );
  for (const line of outcome.failed.slice(0, 10)) {
    console.log(`  失敗: ${line}`);
  }
  if (outcome.locked > 0) {
    console.log("ロックは収集と同時に走ったということ。もう一度流せば取り込まれる。");
  }
  if (outcome.failed.length > 0) {
    process.exitCode = 1;
  }
};

// 使い方の誤りは短い 1 行で返す。スタックトレースを見せても直し方が分からない
await main().catch((cause: unknown) => {
  console.error(cause instanceof Error ? cause.message : String(cause));
  process.exitCode = 2;
});
