/**
 * ジョブの失敗を、URL を持たない形に詰め替える（W1 プラン W1-21 の 2）。
 *
 * 捕捉した例外はそのまま再送出しない。undici の例外は `cause` に URL を抱えることが
 * あるため、`cause` は連結せず、文脈（phase・HTTP ステータス・例外名）と
 * 伏字化したメッセージだけを残す。
 */
import { redact } from "./redact";

/** 失敗した工程。運用調査でどこを見ればよいかが分かる粒度にする。 */
export const JOB_PHASES = ["auth", "validate", "fetch", "parse", "storage"] as const;
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

/** `cause` には触れない。触れると URL が混ざり得る。 */
const messageOf = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

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
