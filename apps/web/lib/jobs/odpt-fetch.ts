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
 * 上限までの距離を数値で監視できる（その読み取りは PR D で追加する）。
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
  /** 受信したバイト列そのもの。再直列化しない（W1 プラン §5 の 14）。 */
  readonly body: Uint8Array;
  /** 認証付きが失敗して公開に切り替えた理由。切り替えていなければ null。伏字化済み。 */
  readonly fallback_reason: string | null;
};

type RequestParams = {
  readonly system_id: SystemId;
  readonly feed: FeedName;
  readonly token: string;
  readonly contact_email: string;
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
const requestHeaders = (contact_email: string): Record<string, string> => ({
  "User-Agent": `${USER_AGENT_PRODUCT} (+${contact_email})`,
  Accept: "application/json",
});

const fetchOnce = async (params: AttemptParams): Promise<OdptFeedResponse> => {
  const received = await fetch(feedUrl(params), {
    headers: requestHeaders(params.contact_email),
    signal: AbortSignal.timeout(ODPT_FETCH_TIMEOUT_MS),
    // トークン付き URL を別ホストへ転送させない。転送は取得失敗として公開へ切り替える。
    redirect: "error",
    cache: "no-store",
  });
  const body = new Uint8Array(await received.arrayBuffer());
  return {
    http_status: received.status,
    endpoint: params.endpoint,
    bytes: body.byteLength,
    body,
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
