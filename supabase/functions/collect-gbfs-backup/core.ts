/**
 * バックアップ収集器の純粋な部分（W3 プラン §5.5、§9.2）。
 *
 * **Deno の API を一切使わない。** ここに置いたものは vitest からそのまま import できて、
 * `apps/web` の `tsc --noEmit` にも掛かる。Deno でしか動かない部分（`Deno.serve`・
 * 環境変数・`CompressionStream`・fetch）は `index.ts` に閉じる。
 *
 * **`packages/shared` を import できない。** Deno は拡張子なしの相対 import を解決せず、
 * `@bikechance/shared` のようなワークスペースの別名も辿れない。そこでパス規約だけを
 * ここに写し、**同じ文字列を返すことを `apps/web/backup-collector-path.test.ts` で
 * 固定する**（片方だけ変えたら CI が落ちる）。
 */

/** 秒精度の POSIX 時刻。GBFS の `last_updated` がこの単位。 */
export type EpochSeconds = number;

/** `packages/shared` の `RAW_BUCKET` と同じでなければならない。 */
export const RAW_BUCKET = "gbfs-raw";

/** `packages/shared` の `GZIP_CONTENT_TYPE` と同じでなければならない。 */
export const GZIP_CONTENT_TYPE = "application/gzip";

/**
 * `packages/shared` の `ODPT_PUBLIC_BASE_URL` と同じでなければならない。
 *
 * **認証付きではなく公開エンドポイントを使う**（W3-04）。CLAUDE.md は「トークン付き
 * URL を組み立てるのは `apps/web/lib/jobs/odpt-fetch.ts` だけ」と定めており、冗長系の
 * ために 2 つ目の組み立て箇所と 2 つ目の秘密の置き場所を作らない。実測（2026-09-08）で
 * 公開エンドポイントは無認証で 200 を返し、応答は認証付きと同一である。
 */
export const ODPT_PUBLIC_BASE_URL = "https://api-public.odpt.org/api/v4/gbfs";

/** `packages/shared` の `SYSTEM_IDS` と同じでなければならない。 */
export const SYSTEM_IDS = ["hellocycling", "docomo-cycle"] as const;
export type SystemId = (typeof SYSTEM_IDS)[number];

/** 保存するフィード。バックアップが扱うのは `station_status` だけ（属性は日次で追いつく）。 */
export const FEED_NAME = "station_status";

/** 同じパスが既にある。**正常系**として扱う（W1 プラン §5 の 15）。 */
export const DUPLICATE_STATUS = 409;

/**
 * 重複だったか。**Storage の REST API は重複を HTTP 400 で返す**（実測 2026-09-08）。
 *
 * ```
 * HTTP/1.1 400 Bad Request
 * {"statusCode":"409","error":"Duplicate","message":"The resource already exists",
 *  "code":"KeyAlreadyExists"}
 * ```
 *
 * 409 は**本文の中**にあり、しかも**文字列**である。HTTP ステータスだけを見ると
 * 「重複」が「想定外の失敗」に化け、毎回 500 を返して `job_runs` が failed で埋まる。
 * `apps/web/lib/jobs/storage.ts` が `statusCode` を string / number 両方で読んでいるのは
 * 同じ理由（supabase-js が本文の値をそのまま載せてくる）。
 *
 * HTTP ステータスの 409 も拾うのは、将来 API が素直になったときに壊れないため。
 */
export const isDuplicateUpload = (http_status: number, body: unknown): boolean => {
  if (http_status === DUPLICATE_STATUS) {
    return true;
  }
  if (typeof body !== "object" || body === null) {
    return false;
  }
  const statusCode: unknown = Reflect.get(body, "statusCode");
  return (
    statusCode === DUPLICATE_STATUS ||
    statusCode === String(DUPLICATE_STATUS) ||
    Reflect.get(body, "code") === "KeyAlreadyExists"
  );
};

export class BackupError extends Error {
  readonly phase: string;
  readonly http_status: number | null;

  /**
   * **応答の断片をメッセージに入れない**（CLAUDE.md §5）。残すのは段・HTTP ステータス・
   * 例外名だけで、これは Vercel 側の `JobError` と同じ規律。
   */
  constructor(params: { phase: string; http_status: number | null; message: string }) {
    super(params.message);
    this.name = "BackupError";
    this.phase = params.phase;
    this.http_status = params.http_status;
  }
}

export const isKnownSystem = (value: string): value is SystemId =>
  SYSTEM_IDS.some((system_id) => system_id === value);

const pad2 = (value: number): string => String(value).padStart(2, "0");

/**
 * `packages/shared/src/storage-path.ts` の `rawObjectPath` と**同じ文字列**を返す。
 *
 *   gbfs-raw/{system_id}/{YYYY}/{MM}/{DD}/{feed}_{epoch_s}.json.gz
 *
 * 日付は **UTC**。同じ観測（同じ `last_updated`）は同じパスに写像されるので、
 * `upsert: false` で保存すれば本番の収集と衝突しても 409 に畳まれる。
 */
export const rawObjectPath = (params: {
  readonly system_id: string;
  readonly feed: string;
  readonly epoch_s: EpochSeconds;
}): string => {
  const at = new Date(params.epoch_s * 1000);
  const year = String(at.getUTCFullYear());
  const month = pad2(at.getUTCMonth() + 1);
  const day = pad2(at.getUTCDate());
  return `${params.system_id}/${year}/${month}/${day}/${params.feed}_${params.epoch_s}.json.gz`;
};

/** ODPT の公開エンドポイント。トークンを付けない。 */
export const statusFeedUrl = (system_id: SystemId): string =>
  `${ODPT_PUBLIC_BASE_URL}/${system_id}/station_status.json`;

/** Storage の 1 オブジェクトの URL。末尾のスラッシュは受けても落とす。 */
export const storageObjectUrl = (supabase_url: string, bucket: string, path: string): string =>
  `${supabase_url.replace(/\/$/, "")}/storage/v1/object/${bucket}/${path}`;

/** PostgREST の RPC の URL。 */
export const rpcUrl = (supabase_url: string, name: string): string =>
  `${supabase_url.replace(/\/$/, "")}/rest/v1/rpc/${name}`;

/**
 * フィードの `last_updated` を読む。**パスを決めるためだけ**に使う。
 *
 * 全文を `JSON.parse` する。4.03 MiB の実データで **26 ms**（Edge Function の CPU 上限
 * 2 秒の 1.3%）なので、正規表現で拾う小細工より素直な方を採る。将来 CPU が問題に
 * なったら `/"last_updated"\s*:\s*(\d+)/` で 0.9 ms に落とせる（実測。現在の応答では
 * この語は 1 回しか現れない）。
 */
export const readLastUpdated = (text: string): EpochSeconds => {
  const document: unknown = JSON.parse(text);
  if (typeof document !== "object" || document === null) {
    throw new BackupError({
      phase: "parse",
      http_status: null,
      message: "応答がオブジェクトでない",
    });
  }
  const value: unknown = Reflect.get(document, "last_updated");
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
    throw new BackupError({
      phase: "parse",
      http_status: null,
      message: "last_updated が正の整数でない",
    });
  }
  return value;
};

/**
 * 文字列を UTF-8 のバイト列にする。
 *
 * `Uint8Array.from` を挟むのは、`TextEncoder.encode` の戻り値が Node の型定義では
 * `Uint8Array<ArrayBufferLike>` になり、`crypto.subtle.digest` が要求する
 * `BufferSource`（`ArrayBufferView<ArrayBuffer>`）に代入できないため。`as` は使わない
 * （CLAUDE.md §3）ので、素直に作り直す。
 */
const toBytes = (value: string): Uint8Array<ArrayBuffer> =>
  Uint8Array.from(new TextEncoder().encode(value));

/** 2 つのバイト列を長さを漏らさずに比べる。長さが違えば false。 */
const equalBytes = (left: Uint8Array, right: Uint8Array): boolean => {
  if (left.length !== right.length) {
    return false;
  }
  return left.reduce((diff, byte, at) => diff | (byte ^ (right[at] ?? 0)), 0) === 0;
};

const sha256 = async (value: string): Promise<Uint8Array> =>
  new Uint8Array(await crypto.subtle.digest("SHA-256", toBytes(value)));

/**
 * `Authorization` ヘッダが `CRON_SECRET` と一致するか。
 *
 * `apps/web/lib/jobs/auth.ts` と同じ規律：**先に SHA-256 で固定長にしてから**定数時間で
 * 比べる（長さ自体も漏らさない）。ヘッダが無い場合と秘密が未設定の場合は常に false。
 * 設定漏れで素通りさせない。
 */
export const isAuthorized = async (
  authorization: string | null,
  cron_secret: string | undefined,
): Promise<boolean> => {
  if (authorization === null || cron_secret === undefined || cron_secret.length === 0) {
    return false;
  }
  const [left, right] = await Promise.all([sha256(authorization), sha256(`Bearer ${cron_secret}`)]);
  return equalBytes(left, right);
};
