/**
 * 天気の 2 つのルート（毎時 17 分のアーカイブ・47 分の取り直し）が使う依存を組み立てる
 * （W12 プランの W12-04）。**同じ保存先・同じ時間切れ**で動かすため、1 か所にまとめる。
 */
import { gzip } from "node:zlib";
import { promisify } from "node:util";
import { WEATHER_BUCKET, WEATHER_DB_TIMEOUT_MS } from "@bikechance/shared";
import type { CatchUpParams } from "./archive-weather";
import { readCollectorEnv } from "./env";
import { createSupabaseUploader } from "./storage";
import { createServiceClient } from "./supabase";
import { createSupabaseWeatherPort } from "./weather-port";

const gzipAsync = promisify(gzip);

/** 環境変数から依存を組み立てる。**足りなければ投げる**（ルートが 500 を返す）。 */
export const weatherJobParams = (
  source: Record<string, string | undefined>,
  now: Date,
): CatchUpParams => {
  const env = readCollectorEnv(source);
  // **DB と Storage の 1 要求にも時間切れ**（W7-03）。時間の予算（`fitsArchiveBudget`）は、
  // 1 分割の最悪を「取得の時間切れ ＋ この時間切れ」として数えている
  const client = createServiceClient({
    url: env.SUPABASE_URL,
    secret_key: env.SUPABASE_SECRET_KEY,
    request_timeout_ms: WEATHER_DB_TIMEOUT_MS,
  });
  return {
    db: createSupabaseWeatherPort(client),
    upload: createSupabaseUploader(client, WEATHER_BUCKET),
    gzip: async (body) => new Uint8Array(await gzipAsync(body)),
    contact_email: env.CONTACT_EMAIL,
    now,
  };
};
