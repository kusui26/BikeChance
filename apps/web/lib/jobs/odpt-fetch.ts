/**
 * ODPT からフィードを取得する。**トークン付き URL を組み立てる唯一の場所**（W1 プラン W1-21 の 1）。
 *
 * ここが守る不変条件：
 *   - 組み立てた URL をモジュールの外へ出さない
 *   - `Response` オブジェクトを外へ返さない（その URL プロパティに触れる経路を作らない）
 *   - 外へ出す文字列は必ず伏字化を通す
 *
 * 認証付きを正、公開をフォールバックにする（開発プラン D-04）。両者の応答は実測で
 * バイト単位まで一致し、ETag も同一。認証付きだけがレート制限の残量ヘッダーを返すため、
 * 上限までの距離を数値で監視できる（W1-21）。
 *
 * `If-None-Match` による条件付き取得（W1-4）：HELLO は 1 分ポーリングの 5 回に 4 回が
 * 同じ内容なので、304 で返してもらえば ODPT からの転送量が 155 MB/日 → 31 MB/日 になる。
 * 開発プラン R9「ODPT への過負荷」への直接的な対策でもある。
 */
import {
  ODPT_FETCH_TIMEOUT_MS,
  ODPT_PUBLIC_BASE_URL,
  ODPT_TOKEN_BASE_URL,
  USER_AGENT_PRODUCT,
  type FeedName,
  type SystemId,
} from "@bikechance/shared";
import { JobError, toJobFailure } from "./errors";
import { redact } from "./redact";

export const ODPT_ENDPOINTS = ["token", "public"] as const;
export type OdptEndpoint = (typeof ODPT_ENDPOINTS)[number];

export type OdptFeedResponse = {
  readonly http_status: number;
  readonly endpoint: OdptEndpoint;
  readonly bytes: number;
  /** 受信したバイト列そのもの。再直列化しない（W1 プラン §5 の 14）。304 のときは空。 */
  readonly body: Uint8Array;
  /** 応答の ETag。次回の条件付き要求に使う。無ければ null。 */
  readonly etag: string | null;
  /**
   * `X-RateLimit-Remaining-day` の値。上限は 24,000 で想定使用量は 2,880（W1-21）。
   * 認証付きエンドポイントだけが返すため、公開へフォールバックしたときは null。
   */
  readonly ratelimit_remaining_day: number | null;
  /** 認証付きが失敗して公開に切り替えた理由。切り替えていなければ null。伏字化済み。 */
  readonly fallback_reason: string | null;
};

type RequestParams = {
  readonly system_id: SystemId;
  readonly feed: FeedName;
  readonly token: string;
  readonly contact_email: string;
  /** 前回取り込みに成功したスナップショットの ETag。渡すと 304 が返り得る。 */
  readonly if_none_match?: string | null;
};

type AttemptParams = RequestParams & { readonly endpoint: OdptEndpoint };

type Attempt =
  | { readonly ok: true; readonly response: OdptFeedResponse }
  | { readonly ok: false; readonly reason: string };

/** 5xx とネットワーク障害だけをフォールバックの対象にする。4xx はそのまま返して気づけるようにする。 */
const SERVER_ERROR_MIN = 500;

/**
 * `acl:consumerKey` はキー名にコロンを含む。`URLSearchParams` はコロンを `%3A` に
 * 変換してしまうため使わず、キーは literal のまま、値だけを encode する。
 */
const feedUrl = (params: AttemptParams): string => {
  const base = params.endpoint === "token" ? ODPT_TOKEN_BASE_URL : ODPT_PUBLIC_BASE_URL;
  const path = `${base}/${params.system_id}/${params.feed}.json`;
  return params.endpoint === "token"
    ? `${path}?acl:consumerKey=${encodeURIComponent(params.token)}`
    : path;
};

/**
 * `Accept-Encoding` は指定しない。undici が既定で gzip を要求し、応答を自動で展開する。
 * 手で指定すると展開の挙動が変わり得るため触らない。保存するのは展開後の原文バイト列。
 */
const requestHeaders = (params: AttemptParams): Record<string, string> => ({
  "User-Agent": `${USER_AGENT_PRODUCT} (+${params.contact_email})`,
  Accept: "application/json",
  ...(params.if_none_match ? { "If-None-Match": params.if_none_match } : {}),
});

/** ヘッダーが非負整数ならその値、それ以外（欠落・非数値）は null。 */
const readNonNegativeInt = (value: string | null): number | null => {
  if (value === null || !/^\d+$/.test(value)) {
    return null;
  }
  return Number(value);
};

const fetchOnce = async (params: AttemptParams): Promise<OdptFeedResponse> => {
  const received = await fetch(feedUrl(params), {
    headers: requestHeaders(params),
    signal: AbortSignal.timeout(ODPT_FETCH_TIMEOUT_MS),
    // トークン付き URL を別ホストへ転送させない。転送は取得失敗として公開へ切り替える。
    redirect: "error",
    // `cache: "no-cache"` を選ぶ理由（実測にもとづく。ここを変えると W1-4 が黙って壊れる）。
    //
    // Fetch 仕様は「要求ヘッダーに If-None-Match があり cache モードが default なら、
    // モードを no-store に切り替える」と定めている。no-store モードでは
    // `Cache-Control: no-cache` と `Pragma: no-cache` が要求に付く。
    // **ODPT は要求の `Cache-Control: no-cache` を見ると条件付き取得を無視して 200 を返す。**
    // つまり `cache` を指定しないか no-store にすると、ETag を送っても常に全文が返り、
    // W1-4 の 80% 転送削減が効かなくなる。エラーもログも出ないので気づけない。
    //
    // 実測（同じ ETag、2026-09-06）：
    //   cache 未指定 / no-store / reload / default → `no-cache` が付き 200
    //   cache: "no-cache"                          → `max-age=0` が付き **304**
    //   cache: "force-cache"                       → 何も付かず 304。ただし Next 側が
    //                                                 応答をキャッシュしてしまうので採らない
    //
    // Next.js 16 のキャッシュは**オプトイン**で、opt-in するのは `force-cache` だけ。
    // `no-cache` は Next のキャッシュに載らない。意味の上でも「毎回オリジンに確認する」で正しい。
    cache: "no-cache",
  });
  const body = new Uint8Array(await received.arrayBuffer());
  return {
    http_status: received.status,
    endpoint: params.endpoint,
    bytes: body.byteLength,
    body,
    etag: received.headers.get("etag"),
    ratelimit_remaining_day: readNonNegativeInt(received.headers.get("x-ratelimit-remaining-day")),
    fallback_reason: null,
  };
};

/** 失敗の理由を、URL を含まない短い文字列にする。 */
const reasonOf = (cause: unknown, token: string): string => {
  const failure = toJobFailure({ phase: "fetch", cause, secrets: [token] });
  return `${failure.error_name}: ${failure.message}`;
};

const attempt = async (params: AttemptParams): Promise<Attempt> => {
  try {
    const response = await fetchOnce(params);
    return response.http_status >= SERVER_ERROR_MIN
      ? { ok: false, reason: `http_${response.http_status}` }
      : { ok: true, response };
  } catch (cause) {
    return { ok: false, reason: reasonOf(cause, params.token) };
  }
};

/**
 * 認証付き →（失敗したら 1 回だけ）公開、の順に取得する。
 * 両方失敗した場合だけ例外を投げる。4xx はフォールバックせず、そのまま呼び出し側へ返す。
 */
export const fetchOdptFeed = async (params: RequestParams): Promise<OdptFeedResponse> => {
  const primary = await attempt({ ...params, endpoint: "token" });
  if (primary.ok) {
    return primary.response;
  }
  const fallback = await attempt({ ...params, endpoint: "public" });
  if (fallback.ok) {
    return { ...fallback.response, fallback_reason: primary.reason };
  }
  throw new JobError({
    phase: "fetch",
    error_name: "OdptUnreachable",
    http_status: null,
    message: redact(`token=[${primary.reason}] public=[${fallback.reason}]`, [params.token]),
  });
};
