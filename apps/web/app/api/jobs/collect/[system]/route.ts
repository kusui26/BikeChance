/**
 * GET /api/jobs/collect/{system} — 生フィードを Storage に保存する（W1 プラン §6.2、PR 0）。
 *
 * Vercel Cron が毎分叩く。DB には一切書かない。PR D がこのルートを育てて、
 * ETag による条件付き取得・重複排除・RPC による取り込みを足す。
 *
 * ステータスコードの方針（§11.6、W1-20）：
 *   200 保存した / 既にあった   401 CRON_SECRET 不一致   400 未知のシステム   500 失敗
 * エラーを 500 にするのは Vercel Observability のエラー率検知を効かせるため。
 * Cron はリダイレクトを追わないので 3xx は返さない（CLAUDE.md §3）。
 */
import { SYSTEM_IDS, type FeedName, type SystemId } from "@bikechance/shared";
import { isAuthorizedCronRequest } from "@/lib/jobs/auth";
import { collectRawFeed, type CollectSummary } from "@/lib/jobs/collect-raw";
import { readCollectorEnv } from "@/lib/jobs/env";
import { toJobFailure } from "@/lib/jobs/errors";
import { createSupabaseUploader } from "@/lib/jobs/storage";
import { createServiceClient } from "@/lib/jobs/supabase";

/**
 * Next.js 16 では Route Handler は**既定でキャッシュされない**ため、
 * `export const dynamic = "force-dynamic"` は不要（同オプションは Cache Components 有効時に
 * 削除される旧 API）。キャッシュに載ると Cron が空振りするので、ビルド出力で
 * このルートが動的（ƒ）であることを毎回確認する。応答にも no-store を付けている。
 *
 * `maxDuration` は**静的なリテラルでなければならない**（定数を import すると Next が
 * 静的解析できず "Invalid segment configuration export" でビルドが落ちる）。
 * `COLLECT_MAX_DURATION_S` と一致していることは route.test.ts で検証する。
 */
export const maxDuration = 60;

/** PR 0 が収集するのは status のみ。station_information は PR F の日次ジョブが扱う。 */
const COLLECTED_FEED: FeedName = "station_status";

const NO_STORE = { "Cache-Control": "no-store" } as const;

type RouteContext = { readonly params: Promise<{ readonly system: string }> };

const isSystemId = (value: string): value is SystemId =>
  SYSTEM_IDS.some((system_id) => system_id === value);

const problem = (status: number, title: string, detail: string): Response =>
  Response.json({ ok: false, title, detail }, { status, headers: NO_STORE });

const summaryResponse = (summary: CollectSummary): Response =>
  Response.json(summary, { status: summary.ok ? 200 : 500, headers: NO_STORE });

export const GET = async (request: Request, context: RouteContext): Promise<Response> => {
  // 1. 認証。DB にもログにも何も書かずに弾く（スキャナの試行で記録を埋めない）
  if (!isAuthorizedCronRequest(request.headers.get("authorization"), process.env.CRON_SECRET)) {
    return problem(401, "unauthorized", "CRON_SECRET が一致しません。");
  }

  // 2. パスの検証
  const { system } = await context.params;
  if (!isSystemId(system)) {
    return problem(400, "unknown_system", `未知のシステムです: ${system}`);
  }

  // 3. 環境変数 → 取得 → gzip → Storage
  //    設定漏れも 500 にする。メッセージには変数名しか載らない（env.ts）
  try {
    const env = readCollectorEnv(process.env);
    const summary = await collectRawFeed({
      upload: createSupabaseUploader(
        createServiceClient({ url: env.SUPABASE_URL, secret_key: env.SUPABASE_SECRET_KEY }),
      ),
      system_id: system,
      feed: COLLECTED_FEED,
      token: env.ODPT_ACCESS_TOKEN,
      contact_email: env.CONTACT_EMAIL,
      now: new Date(),
    });
    return summaryResponse(summary);
  } catch (cause) {
    const failure = toJobFailure({ phase: "validate", cause });
    return Response.json(
      { ok: false, system, result: "error", error: failure },
      { status: 500, headers: NO_STORE },
    );
  }
};
