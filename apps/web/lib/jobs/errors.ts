/**
 * ジョブの失敗を、URL を持たない形に詰め替える（W1 プラン W1-21 の 2）。
 *
 * 捕捉した例外はそのまま再送出しない。undici の例外は `cause` に URL を抱えることが
 * あるため、`cause` は連結せず、文脈（phase・HTTP ステータス・例外名）と
 * 伏字化したメッセージだけを残す。
 */
import { redact } from "./redact";

/** 失敗した工程。運用調査でどこを見ればよいかが分かる粒度にする。 */
export const JOB_PHASES = [
  "auth",
  "validate",
  "fetch",
  "parse",
  "storage",
  "ingest",
  "unknown",
] as const;
export type JobPhase = (typeof JOB_PHASES)[number];

export type JobFailure = {
  readonly phase: JobPhase;
  /** 例外の名前（`TimeoutError` など）。型ではなく値の分類に使う。 */
  readonly error_name: string;
  /** 上流の HTTP ステータス。HTTP を伴わない失敗では null。 */
  readonly http_status: number | null;
  /** 伏字化済みのメッセージ。**URL とトークンは含まない。** */
  readonly message: string;
};

const nameOf = (cause: unknown): string =>
  cause instanceof Error ? cause.name : `Non-Error(${typeof cause})`;

/**
 * `Error` ではないが、`message`（文字列）を持つ失敗。supabase-js の `{ data, error }` の
 * `error` がこれにあたる——PostgREST の本文（`{ code, message, details, hint }`）を
 * ふつうのオブジェクトのまま返すので、`String()` にすると `[object Object]` になっていた
 * （W7 プランの W7-04。収集の `begin_fetch`・属性の同期・天気の RPC）。
 */
type MessageBearing = { readonly message: string; readonly code?: unknown };

const isMessageBearing = (value: unknown): value is MessageBearing =>
  typeof value === "object" &&
  value !== null &&
  "message" in value &&
  typeof value.message === "string";

/** ふつうのオブジェクトから拾う言葉の上限。本文が HTML の失敗のページのまま入ることがある。 */
const PLAIN_MESSAGE_MAX_CHARS = 300;

/** `code: message`（`code` が空なら `message` だけ）。 */
const plainMessageOf = (cause: MessageBearing): string => {
  const message = cause.message.slice(0, PLAIN_MESSAGE_MAX_CHARS);
  return typeof cause.code === "string" && cause.code !== ""
    ? `${cause.code}: ${message}`
    : message;
};

/**
 * `cause` には触れない。触れると URL が混ざり得る。**ふつうのオブジェクトは `message` と
 * `code` だけを拾う**——`details` には通信の失敗の原因と `stack`（接続先が出る）が入り、
 * `hint` は要求の URL の長さに触れるので読まない。
 */
const messageOf = (cause: unknown): string => {
  if (cause instanceof Error) {
    return cause.message;
  }
  return isMessageBearing(cause) ? plainMessageOf(cause) : String(cause);
};

/** 例外を `JobFailure` に詰め替える。記録・応答にはこの結果だけを使う。 */
export const toJobFailure = (params: {
  readonly phase: JobPhase;
  readonly cause: unknown;
  readonly http_status?: number | null;
  readonly secrets?: readonly string[];
}): JobFailure => ({
  phase: params.phase,
  error_name: nameOf(params.cause),
  http_status: params.http_status ?? null,
  message: redact(messageOf(params.cause), params.secrets ?? []),
});

/** 上流が異常なステータスを返した（例外ではない）ときの失敗。 */
export const httpJobFailure = (params: {
  readonly phase: JobPhase;
  readonly http_status: number;
  readonly detail: string;
}): JobFailure => ({
  phase: params.phase,
  error_name: "UnexpectedHttpStatus",
  http_status: params.http_status,
  message: redact(params.detail),
});

/** 失敗を運ぶ例外。メッセージは既に伏字化済みのものだけを持つ。 */
export class JobError extends Error {
  readonly failure: JobFailure;

  constructor(failure: JobFailure) {
    super(`${failure.phase}: ${failure.error_name}`);
    this.name = "JobError";
    this.failure = failure;
  }
}

export const isJobError = (value: unknown): value is JobError => value instanceof JobError;
