/**
 * 生フィードを gzip して Storage に保存する（W1 プラン §6.2 の手順 5、§11.5）。
 *
 * 保存するのは **ODPT から受信したバイト列そのもの**。パース後に再直列化すると
 * 原文と一致しなくなる（§5 の 14）。
 *
 * 同じ観測は同じパスに写像されるため、`upsert: false` で保存すれば重複は 409 になる。
 * これは正常系として扱う（§5 の 15）。結果として、条件付き要求を使わない PR 0 でも
 * Storage のオブジェクト数は重複排除した場合と一致する。
 *
 * Storage への書き込みは `RawUploader` という 1 つの関数に絞ってある。こうすると
 * 上位のロジックが supabase-js に依存せず、テストで差し替えられる。
 */
import { gzip } from "node:zlib";
import { promisify } from "node:util";
import {
  GZIP_CONTENT_TYPE,
  RAW_BUCKET,
  rawObjectPath,
  type EpochSeconds,
  type FeedName,
  type SystemId,
} from "@bikechance/shared";
import type { SupabaseClient } from "@supabase/supabase-js";
import { JobError, isJobError, toJobFailure } from "./errors";

const gzipAsync = promisify(gzip);

const DUPLICATE_STATUS_CODE = 409;

export const RAW_SAVE_RESULTS = ["saved", "duplicate"] as const;
export type RawSaveResult = (typeof RAW_SAVE_RESULTS)[number];

export type RawSaveOutcome = {
  readonly result: RawSaveResult;
  readonly path: string;
  readonly gzip_bytes: number;
};

/**
 * Storage への書き込みの差し替え点。
 * 既に同じパスがあれば `duplicate: true` を返し、それ以外の失敗は例外にする。
 */
export type RawUploader = (params: {
  readonly path: string;
  readonly gzipped: Uint8Array;
}) => Promise<{ readonly duplicate: boolean }>;

/** エラーオブジェクトから数値のステータスを読む。版によって型が string / number で揺れる。 */
const readStatus = (error: unknown, key: string): number | null => {
  if (typeof error !== "object" || error === null || !(key in error)) {
    return null;
  }
  const value: unknown = Reflect.get(error, key);
  if (typeof value === "number") {
    return value;
  }
  return typeof value === "string" && /^\d+$/.test(value) ? Number(value) : null;
};

const isDuplicateError = (error: unknown): boolean =>
  readStatus(error, "statusCode") === DUPLICATE_STATUS_CODE ||
  readStatus(error, "status") === DUPLICATE_STATUS_CODE;

/** 本番で使う実装。サービスロールのクライアントを 1 つの関数に包む。 */
export const createSupabaseUploader =
  (client: SupabaseClient): RawUploader =>
  async ({ path, gzipped }) => {
    const { error } = await client.storage
      .from(RAW_BUCKET)
      .upload(path, gzipped, { contentType: GZIP_CONTENT_TYPE, upsert: false });
    if (error === null) {
      return { duplicate: false };
    }
    if (isDuplicateError(error)) {
      return { duplicate: true };
    }
    throw new JobError(toJobFailure({ phase: "storage", cause: error }));
  };

/**
 * 失敗は必ず `storage` フェーズの JobError にして返す。
 * 素の例外のまま上げると、呼び出し側でどの段の失敗か分からなくなる。
 */
export const saveRawFeed = async (params: {
  readonly upload: RawUploader;
  readonly system_id: SystemId;
  readonly feed: FeedName;
  readonly epoch_s: EpochSeconds;
  readonly body: Uint8Array;
}): Promise<RawSaveOutcome> => {
  const path = rawObjectPath({
    system_id: params.system_id,
    feed: params.feed,
    epoch_s: params.epoch_s,
  });
  try {
    const gzipped = await gzipAsync(params.body);
    const { duplicate } = await params.upload({ path, gzipped });
    return { result: duplicate ? "duplicate" : "saved", path, gzip_bytes: gzipped.byteLength };
  } catch (cause) {
    throw isJobError(cause) ? cause : new JobError(toJobFailure({ phase: "storage", cause }));
  }
};
