/**
 * 内閣府「国民の祝日について」CSV を `jp_holidays` に取り込む（W3 プラン §5.6）。
 *
 * **CSV は Shift_JIS。** UTF-8 として読むと名称が化けるが、日付だけは読めてしまうので
 * 気づきにくい。明示的にデコードし、既知の名称と一致するかを確かめてから入れる。
 *
 * **年末年始（12/29〜1/3）とお盆（8/13〜16）は入れない。** 暦の規則で決まるので
 * `packages/shared/src/calendar.ts` が持つ（W3 プラン §12 の 87）。
 *
 * 入れ替えは `replace_jp_holidays` RPC で 1 トランザクション。delete と insert を
 * 分けると、その間だけ祝日が 0 件になり、そこで特徴量を作ると全部平日になる。
 *
 * **条件付き取得は使えない**（実測）。内閣府のサーバは `etag` と `last-modified` を
 * 返すが、`If-None-Match` も `If-Modified-Since` も 200 を返す。しかも **ETag は同じ
 * ファイルでも要求ごとに変わる**（負荷分散された別のサーバが別の mtime を持っている）。
 * そこで**中身のハッシュを自分で比べる**。21 KB のダウンロードは毎回払うが、それだけ。
 * 月次で回す想定（W3 プラン §12 の 89）。
 *
 * 使い方:
 *   pnpm exec tsx scripts/import-holidays.ts <環境ファイル> [--force] [--dry-run]
 *   pnpm exec tsx scripts/import-holidays.ts .env --dry-run
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv";
/** 1955 年からの収録で 1,000 行以上あるのが正常。下回ったら取得が壊れている。 */
const MIN_ROWS = 100;
/** デコードの確認に使う。**この 3 つは 1955 年から毎年ある。** */
const KNOWN_NAMES = ["元日", "成人の日", "文化の日"] as const;

type Env = Readonly<Record<string, string>>;
type Holiday = { readonly d: string; readonly n: string };

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

const require_ = (env: Env, name: string): string => {
  const value = env[name];
  if (value === undefined || value === "") {
    // **値は出さない。** 足りないときに出すのは変数名だけ（CLAUDE.md §5）
    throw new Error(`環境変数が足りません: ${name}`);
  }
  return value;
};

const headers = (env: Env): Record<string, string> => {
  const key = require_(env, "SUPABASE_SECRET_KEY");
  return { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json" };
};

const restUrl = (env: Env, path: string): string =>
  `${require_(env, "SUPABASE_URL").replace(/\/$/, "")}/rest/v1${path}`;

/** `app_config` の 1 行を読む。無ければ `null`。 */
const readConfig = async (env: Env, key: string): Promise<string | null> => {
  const received = await fetch(restUrl(env, `/app_config?key=eq.${key}&select=value`), {
    headers: headers(env),
  });
  const rows: unknown = await received.json();
  const first = Array.isArray(rows) ? rows[0] : undefined;
  const value = typeof first === "object" && first !== null ? Reflect.get(first, "value") : null;
  return typeof value === "string" ? value : null;
};

const writeConfig = async (env: Env, key: string, value: string): Promise<void> => {
  const received = await fetch(restUrl(env, "/app_config?on_conflict=key"), {
    method: "POST",
    headers: { ...headers(env), Prefer: "resolution=merge-duplicates" },
    body: JSON.stringify([{ key, value, updated_at: new Date().toISOString() }]),
  });
  if (!received.ok) {
    const failure: unknown = await received.json().catch(() => null);
    const reason =
      typeof failure === "object" && failure !== null
        ? String(Reflect.get(failure, "message") ?? "")
        : "(本文なし)";
    throw new Error(`app_config.${key} を書けません（HTTP ${received.status}）: ${reason}`);
  }
};

/**
 * CSV をパースする。**日付は 1 桁の月日で書かれている**（`2026/1/1`）ので
 * ゼロ詰めしてから ISO にする。
 */
const parseCsv = (body: Uint8Array): readonly Holiday[] => {
  const text = new TextDecoder("shift_jis").decode(body);
  const rows: Holiday[] = [];
  for (const line of text.split(/\r?\n/).slice(1)) {
    if (line.trim() === "") continue;
    const [date, name] = line.split(",");
    if (date === undefined || name === undefined) continue;
    const [year, month, day] = date.split("/");
    if (year === undefined || month === undefined || day === undefined) continue;
    rows.push({
      d: `${year}-${month.padStart(2, "0")}-${day.padStart(2, "0")}`,
      n: name.trim(),
    });
  }
  return rows;
};

/** 取り込む前に形を確かめる。**壊れた CSV で表を置き換えない。** */
const validate = (rows: readonly Holiday[]): void => {
  if (rows.length < MIN_ROWS) {
    throw new Error(`行が少なすぎます: ${rows.length}`);
  }
  const bad = rows.find((row) => !/^\d{4}-\d{2}-\d{2}$/.test(row.d) || row.n === "");
  if (bad !== undefined) {
    throw new Error(`読めない行があります: ${bad.d}`);
  }
  const dates = new Set(rows.map((row) => row.d));
  if (dates.size !== rows.length) {
    throw new Error("同じ日付が 2 回出てきます");
  }
  // **Shift_JIS を UTF-8 として読むと名称が化ける。** 日付だけは読めるので気づきにくい
  const names = new Set(rows.map((row) => row.n));
  const missing = KNOWN_NAMES.filter((known) => !names.has(known));
  if (missing.length > 0) {
    throw new Error(`既知の祝日名が見つかりません（デコードの失敗？）: ${missing.join(", ")}`);
  }
};

/**
 * 中身のハッシュ。**サーバの ETag は当てにできない**（同じファイルでも要求ごとに変わる）
 * ので、正規化した行から自分で作る。並び順に依存しないよう日付で並べてから畳む。
 */
const contentDigest = (rows: readonly Holiday[]): string =>
  createHash("sha256")
    .update(
      [...rows]
        .sort((left, right) => left.d.localeCompare(right.d))
        .map((row) => `${row.d},${row.n}`)
        .join("\n"),
      "utf8",
    )
    .digest("hex");

const summarize = (rows: readonly Holiday[]): string => {
  const byYear = new Map<string, number>();
  for (const row of rows) {
    const year = row.d.slice(0, 4);
    byYear.set(year, (byYear.get(year) ?? 0) + 1);
  }
  const years = [...byYear.keys()].sort();
  const recent = years.slice(-3).map((year) => `${year} 年 ${byYear.get(year)} 件`);
  return `${rows.length} 件（${years[0]} 〜 ${years.at(-1)}）／ ${recent.join("・")}`;
};

const main = async (): Promise<void> => {
  const args = process.argv.slice(2);
  const [envPath] = args.filter((one) => !one.startsWith("--"));
  if (envPath === undefined) {
    throw new Error(
      "使い方: pnpm exec tsx scripts/import-holidays.ts <環境ファイル> [--force] [--dry-run]",
    );
  }
  const env = readEnv(envPath);
  const force = args.includes("--force");
  const dryRun = args.includes("--dry-run");

  const received = await fetch(CSV_URL, {
    headers: { "User-Agent": "BikeChance/0.1 (holidays import)" },
  });
  if (received.status !== 200) {
    throw new Error(`内閣府 CSV が ${received.status} を返しました`);
  }

  const rows = parseCsv(new Uint8Array(await received.arrayBuffer()));
  validate(rows);
  const digest = contentDigest(rows);
  console.log(`取得: ${summarize(rows)}`);
  console.log(`last-modified: ${received.headers.get("last-modified") ?? "(不明)"}`);
  console.log(`中身のハッシュ: ${digest.slice(0, 16)}…`);

  const known = force ? null : await readConfig(env, "holidays_sha256");
  if (known === digest) {
    console.log("中身が前回と同じなので何も書きません（--force で強制できます）。");
    return;
  }

  if (dryRun) {
    console.log("--dry-run のため書き込みません。");
    return;
  }

  const applied = await fetch(restUrl(env, "/rpc/replace_jp_holidays"), {
    method: "POST",
    headers: headers(env),
    body: JSON.stringify({ p_rows: rows }),
  });
  if (!applied.ok) {
    // **理由を出す。** ステータスだけだと「行が少なすぎる」のか「関数が無い」のか
    // 分からない。PostgREST の本文は自分たちのエラー文なので、そのまま出してよい
    const failure: unknown = await applied.json().catch(() => null);
    const reason =
      typeof failure === "object" && failure !== null
        ? `${String(Reflect.get(failure, "code") ?? "")} ${String(Reflect.get(failure, "message") ?? "")}`
        : "(本文なし)";
    throw new Error(`replace_jp_holidays が失敗しました（HTTP ${applied.status}）: ${reason}`);
  }
  console.log(`入れ替え: ${JSON.stringify(await applied.json())}`);

  await writeConfig(env, "holidays_sha256", digest);
  await writeConfig(env, "holidays_last_modified", received.headers.get("last-modified") ?? "");
  await writeConfig(env, "holidays_imported_at", new Date().toISOString());
  console.log("完了。");
};

await main();
