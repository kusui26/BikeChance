/**
 * 既存の Parquet を畳み直す（W3 プラン §5.4、段 2）。
 *
 * **列を足しただけで再圧縮しないと、読む側が静かに壊れる。** pyarrow は
 * スキーマが混在するとき、**最初に見つけたファイル**からスキーマを推論して
 * 足りない列を黙って落とす。しかも「最初」はディレクトリの列挙順なので環境で変わる
 * （W2 プラン §14.3 の実測）。だからスキーマ変更と再圧縮は同じ変更で行う。
 *
 * 再圧縮は `status_snapshots`（60 日保持）から読むので、**費用は時間とともに増える**。
 * 保持期間を超えた月は先に `rebuild-snapshots.ts` で生 JSON から戻す必要がある
 * （2 段階になる）。9 月分の期限は 2026-11-30。
 *
 * `/ml/compact?hour=` は同じ時間帯を同じパスに上書きするので、**何度実行しても
 * 結果は変わらない**。途中で落ちたら同じ範囲をもう一度流せばよい。
 *
 * 使い方:
 *   pnpm exec tsx scripts/recompact-parquet.ts <環境ファイル> <開始> [終了] [--dry-run]
 *   pnpm exec tsx scripts/recompact-parquet.ts .env 2026-09-06T06:00:00Z
 *   pnpm exec tsx scripts/recompact-parquet.ts .env 2026-09-06T06:00:00Z 2026-09-06T09:00:00Z
 *
 * 時刻は **UTC の正時**（Parquet のパスと同じ）。開始・終了とも含む。終了を省くと
 * 「直前の完全な 1 時間」まで。
 */
import { readFileSync } from "node:fs";

const HOUR_MS = 3_600_000;
/** 1 回だけ試し直す。連続で失敗するなら原因が別にある。 */
const RETRY_DELAY_MS = 2_000;
/** 呼び出しの間隔。毎時ジョブと同じ処理なので、詰めて叩く必要が無い。 */
const GAP_MS = 200;

type Env = Readonly<Record<string, string>>;

/** 1 時間ぶんの結果。表示と集計に要るものだけ持つ。 */
type HourOutcome = {
  readonly hour: string;
  readonly ok: boolean;
  readonly http_status: number;
  readonly n_rows: number;
  readonly bytes: number;
  readonly duration_ms: number;
  readonly note: string;
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

const require_ = (env: Env, name: string): string => {
  const value = env[name];
  if (value === undefined || value === "") {
    // **値は出さない。** 足りないときに出すのは変数名だけ（CLAUDE.md §5）
    throw new Error(`環境変数が足りません: ${name}`);
  }
  return value;
};

/** UTC の正時に丸まっていることを確かめる。ずれた時刻は別の区間を指す。 */
const parseHour = (text: string, label: string): Date => {
  const at = new Date(text);
  if (Number.isNaN(at.getTime())) {
    throw new Error(`${label} を ISO 8601 として読めません（例 2026-09-06T06:00:00Z）: ${text}`);
  }
  if (at.getTime() % HOUR_MS !== 0) {
    throw new Error(`${label} は UTC の正時で指定してください: ${text}`);
  }
  return at;
};

/** 直前の完全な 1 時間の開始時刻。まだ終わっていない時間帯は畳めない。 */
const lastCompleteHour = (now: Date): Date =>
  new Date(Math.floor(now.getTime() / HOUR_MS) * HOUR_MS - HOUR_MS);

const hoursBetween = (from: Date, to: Date): readonly Date[] => {
  const hours: Date[] = [];
  for (let at = from.getTime(); at <= to.getTime(); at += HOUR_MS) {
    hours.push(new Date(at));
  }
  return hours;
};

/** 応答の JSON から、表示に使う項目だけを型ガードで取り出す。 */
const readSummary = (body: unknown): { ok: boolean; n_rows: number; bytes: number; ms: number } => {
  if (typeof body !== "object" || body === null) {
    return { ok: false, n_rows: 0, bytes: 0, ms: 0 };
  }
  const num = (name: string): number => {
    const value = Reflect.get(body, name);
    return typeof value === "number" ? value : 0;
  };
  return {
    ok: Reflect.get(body, "ok") === true,
    n_rows: num("n_rows"),
    bytes: num("bytes"),
    ms: num("duration_ms"),
  };
};

const sleep = (ms: number): Promise<void> => new Promise((done) => setTimeout(done, ms));

/** `app_config.project_base_url` を正とする。ウォッチドッグが叩く先と同じにする。 */
const readBaseUrl = async (env: Env): Promise<string> => {
  const override = env["BASE_URL"];
  if (override !== undefined && override !== "") {
    return override.replace(/\/$/, "");
  }
  const key = require_(env, "SUPABASE_SECRET_KEY");
  const url = `${require_(env, "SUPABASE_URL").replace(/\/$/, "")}/rest/v1/app_config?key=eq.project_base_url&select=value`;
  const received = await fetch(url, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
  const rows: unknown = await received.json();
  const first = Array.isArray(rows) ? rows[0] : undefined;
  const value =
    typeof first === "object" && first !== null ? Reflect.get(first, "value") : undefined;
  if (typeof value !== "string" || value === "") {
    throw new Error("app_config.project_base_url が読めません（BASE_URL で上書きできます）");
  }
  return value.replace(/\/$/, "");
};

const compactOnce = async (base: string, secret: string, hour: string): Promise<HourOutcome> => {
  const received = await fetch(`${base}/ml/compact?hour=${encodeURIComponent(hour)}`, {
    headers: { Authorization: `Bearer ${secret}` },
    redirect: "error",
  });
  const body: unknown = await received.json().catch(() => null);
  const summary = readSummary(body);
  return {
    hour,
    ok: received.status === 200 && summary.ok,
    http_status: received.status,
    n_rows: summary.n_rows,
    bytes: summary.bytes,
    duration_ms: summary.ms,
    note: "",
  };
};

/** 例外を握って `null` にする。1 時間の失敗で 70 時間ぶんを落とさない。 */
const attempt = async (base: string, secret: string, hour: string): Promise<HourOutcome | null> => {
  try {
    return await compactOnce(base, secret, hour);
  } catch {
    return null;
  }
};

/** 1 度だけ試し直す。連続で失敗するなら原因が別にあるので、そこで止める。 */
const compactHour = async (base: string, secret: string, hour: string): Promise<HourOutcome> => {
  const first = await attempt(base, secret, hour);
  if (first !== null && first.ok) {
    return first;
  }
  await sleep(RETRY_DELAY_MS);
  const second = await attempt(base, secret, hour);
  if (second === null) {
    return {
      hour,
      ok: false,
      http_status: 0,
      n_rows: 0,
      bytes: 0,
      duration_ms: 0,
      note: "2 回とも例外",
    };
  }
  return { ...second, note: second.ok ? "再試行で成功" : "2 回とも失敗" };
};

const line = (outcome: HourOutcome): string =>
  [
    outcome.ok ? "  ok " : "  NG ",
    outcome.hour,
    ` HTTP ${outcome.http_status}`,
    ` ${outcome.n_rows.toLocaleString("en-US").padStart(9)} 行`,
    ` ${Math.round(outcome.bytes / 1024)
      .toLocaleString("en-US")
      .padStart(6)} KiB`,
    ` ${String(outcome.duration_ms).padStart(6)} ms`,
    outcome.note === "" ? "" : ` (${outcome.note})`,
  ].join("");

const main = async (): Promise<void> => {
  const args = process.argv.slice(2);
  const dryRun = args.includes("--dry-run");
  const positional = args.filter((one) => !one.startsWith("--"));
  const [envPath, fromText, toText] = positional;
  if (envPath === undefined || fromText === undefined) {
    throw new Error(
      "使い方: pnpm exec tsx scripts/recompact-parquet.ts <環境ファイル> <開始> [終了] [--dry-run]",
    );
  }

  const env = readEnv(envPath);
  const from = parseHour(fromText, "開始");
  const to = toText === undefined ? lastCompleteHour(new Date()) : parseHour(toText, "終了");
  if (to < from) throw new Error("終了が開始より前です");

  const hours = hoursBetween(from, to);
  const base = await readBaseUrl(env);
  console.log(
    `対象 ${hours.length} 時間  ${from.toISOString()} 〜 ${to.toISOString()}（UTC・両端を含む）`,
  );
  console.log(`宛先 ${base}/ml/compact`);
  if (dryRun) {
    console.log("--dry-run のため呼び出しません。");
    return;
  }

  const secret = require_(env, "CRON_SECRET");
  const outcomes: HourOutcome[] = [];
  for (const at of hours) {
    const outcome = await compactHour(base, secret, at.toISOString().replace(".000", ""));
    outcomes.push(outcome);
    console.log(line(outcome));
    await sleep(GAP_MS);
  }

  const failed = outcomes.filter((one) => !one.ok);
  const rows = outcomes.reduce((sum, one) => sum + one.n_rows, 0);
  const bytes = outcomes.reduce((sum, one) => sum + one.bytes, 0);
  console.log(
    `\n完了 ${outcomes.length - failed.length}/${outcomes.length} 時間  ` +
      `${rows.toLocaleString("en-US")} 行  ${(bytes / 1024 / 1024).toFixed(2)} MiB`,
  );
  if (failed.length > 0) {
    console.log(`失敗した時間帯: ${failed.map((one) => one.hour).join(", ")}`);
    process.exitCode = 1;
  }
};

await main();
