/**
 * Cron ハンドラの認証（W1 プラン §6.2 の手順 1、CLAUDE.md §3 の Cron 定型）。
 *
 * Vercel Cron は本番デプロイに対して `Authorization: Bearer <CRON_SECRET>` を自動で付ける。
 * pg_cron のウォッチドッグ（PR E1）も同じヘッダで叩く。
 *
 * 比較は定数時間で行う。`timingSafeEqual` は長さが違うと例外になるため、
 * 先に SHA-256 で固定長にしてから比較する。これで長さ自体も漏れない。
 */
import { createHash, timingSafeEqual } from "node:crypto";

const BEARER_PREFIX = "Bearer ";

const digest = (value: string): Buffer => createHash("sha256").update(value, "utf8").digest();

/**
 * `Authorization` ヘッダが `CRON_SECRET` と一致するか。
 * ヘッダが無い場合と、秘密が未設定の場合は常に false（設定漏れで素通りさせない）。
 */
export const isAuthorizedCronRequest = (
  authorization: string | null,
  cron_secret: string | undefined,
): boolean => {
  if (authorization === null || cron_secret === undefined || cron_secret.length === 0) {
    return false;
  }
  return timingSafeEqual(digest(authorization), digest(`${BEARER_PREFIX}${cron_secret}`));
};
