/**
 * GET /api/jobs/archive-weather — 予報の生 JSON を毎時アーカイブする（W2 プラン §5.3、PR A）。
 *
 * 手順そのものは `lib/jobs/archive-weather.ts` にある。ここは認証と依存の組み立て、
 * 状態コードの割り当てだけを行う（collect / sync-stations と同じ形）。
 *
 * システム別ではないので `[system]` を取らない。天気は事業者に依らない。
 *
 * ステータスコードの方針（W1 プラン §11.6、W1-20）：
 *   200 成功        401 CRON_SECRET 不一致       500 失敗
 * エラーを 500 にするのは Vercel Observability のエラー率検知を効かせるため。
 * Cron はリダイレクトを追わないので 3xx は返さない（CLAUDE.md §3）。
 */
import { gzip } from "node:zlib";
import { promisify } from "node:util";
import { WEATHER_BUCKET } from "@bikechance/shared";
import { archiveWeather, type ArchiveWeatherSummary } from "@/lib/jobs/archive-weather";
import { isAuthorizedCronRequest } from "@/lib/jobs/auth";
import { readCollectorEnv } from "@/lib/jobs/env";
import { toJobFailure } from "@/lib/jobs/errors";
import { createSupabaseUploader } from "@/lib/jobs/storage";
import { createServiceClient } from "@/lib/jobs/supabase";
import { createSupabaseWeatherPort } from "@/lib/jobs/weather-port";

/**
 * `maxDuration` は**静的なリテラルでなければならない**（定数を import すると Next が
 * 静的解析できず "Invalid segment configuration export" でビルドが落ちる）。
 * `ARCHIVE_WEATHER_MAX_DURATION_S` と一致していることは route.test.ts で検証する。
 *
 * 実測では 100 地点 48 時間の 1 要求が 2.0 秒で、595 格子なら 6 要求で約 12 秒。
 * 1 要求 15 秒のタイムアウトを 6 回踏んでも収まる値にしてある。
 */
export const maxDuration = 120;

const NO_STORE = { "Cache-Control": "no-store" } as const;

const gzipAsync = promisify(gzip);

const problem = (status: number, title: string, detail: string): Response =>
  Response.json({ ok: false, title, detail }, { status, headers: NO_STORE });

const summaryResponse = (summary: ArchiveWeatherSummary): Response =>
  Response.json(summary, { status: summary.ok ? 200 : 500, headers: NO_STORE });

export const GET = async (request: Request): Promise<Response> => {
  // 1. 認証。DB にもログにも何も書かずに弾く
  if (!isAuthorizedCronRequest(request.headers.get("authorization"), process.env.CRON_SECRET)) {
    return problem(401, "unauthorized", "CRON_SECRET が一致しません。");
  }

  // 2. 環境変数 → 格子の取得 → 予報の取得 → 保存 → 記録
  try {
    const env = readCollectorEnv(process.env);
    const client = createServiceClient({
      url: env.SUPABASE_URL,
      secret_key: env.SUPABASE_SECRET_KEY,
    });
    const summary = await archiveWeather({
      db: createSupabaseWeatherPort(client),
      upload: createSupabaseUploader(client, WEATHER_BUCKET),
      gzip: async (body) => new Uint8Array(await gzipAsync(body)),
      contact_email: env.CONTACT_EMAIL,
      now: new Date(),
    });
    return summaryResponse(summary);
  } catch (cause) {
    const failure = toJobFailure({ phase: "validate", cause });
    return Response.json({ ok: false, error: failure }, { status: 500, headers: NO_STORE });
  }
};
