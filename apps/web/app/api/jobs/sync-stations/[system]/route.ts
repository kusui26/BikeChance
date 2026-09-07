/**
 * GET /api/jobs/sync-stations/{system} — station_information を日次で取り込む（§6.9、PR F）。
 *
 * 手順そのものは `lib/jobs/sync-stations.ts` にある。ここは認証と入力の検証、
 * 依存の組み立て、状態コードの割り当てだけを行う（collect のルートと同じ形）。
 *
 * ステータスコードの方針（§11.6、W1-20）：
 *   200 ok / locked        401 CRON_SECRET 不一致
 *   400 未知のシステム      500 失敗
 * エラーを 500 にするのは Vercel Observability のエラー率検知を効かせるため。
 * Cron はリダイレクトを追わないので 3xx は返さない（CLAUDE.md §3）。
 */
import { SYSTEM_IDS, type SystemId } from "@bikechance/shared";
import { isAuthorizedCronRequest } from "@/lib/jobs/auth";
import { createSupabaseAttributesPort } from "@/lib/jobs/attributes-port";
import { readCollectorEnv } from "@/lib/jobs/env";
import { toJobFailure } from "@/lib/jobs/errors";
import { createSupabaseUploader } from "@/lib/jobs/storage";
import { createServiceClient } from "@/lib/jobs/supabase";
import { syncStations, type SyncStationsSummary } from "@/lib/jobs/sync-stations";

/**
 * `maxDuration` は**静的なリテラルでなければならない**（定数を import すると Next が
 * 静的解析できず "Invalid segment configuration export" でビルドが落ちる）。
 * `SYNC_STATIONS_MAX_DURATION_S` と一致していることは route.test.ts で検証する。
 *
 * 収集の 60 秒より長くとる。HELLO の station_information は 7.8 MB あり、
 * 取得・展開・14,900 件の比較を 1 回で行うため。
 */
export const maxDuration = 120;

const NO_STORE = { "Cache-Control": "no-store" } as const;

type RouteContext = { readonly params: Promise<{ readonly system: string }> };

const isSystemId = (value: string): value is SystemId =>
  SYSTEM_IDS.some((system_id) => system_id === value);

const problem = (status: number, title: string, detail: string): Response =>
  Response.json({ ok: false, title, detail }, { status, headers: NO_STORE });

const summaryResponse = (summary: SyncStationsSummary): Response =>
  Response.json(summary, { status: summary.ok ? 200 : 500, headers: NO_STORE });

export const GET = async (request: Request, context: RouteContext): Promise<Response> => {
  // 1. 認証。DB にもログにも何も書かずに弾く
  if (!isAuthorizedCronRequest(request.headers.get("authorization"), process.env.CRON_SECRET)) {
    return problem(401, "unauthorized", "CRON_SECRET が一致しません。");
  }

  // 2. パスの検証
  const { system } = await context.params;
  if (!isSystemId(system)) {
    return problem(400, "unknown_system", `未知のシステムです: ${system}`);
  }

  // 3. 環境変数 → 取得 → 保存 → 取り込み
  try {
    const env = readCollectorEnv(process.env);
    const client = createServiceClient({
      url: env.SUPABASE_URL,
      secret_key: env.SUPABASE_SECRET_KEY,
    });
    const summary = await syncStations({
      db: createSupabaseAttributesPort(client),
      upload: createSupabaseUploader(client),
      system_id: system,
      token: env.ODPT_ACCESS_TOKEN,
      contact_email: env.CONTACT_EMAIL,
      now: new Date(),
    });
    return summaryResponse(summary);
  } catch (cause) {
    const failure = toJobFailure({ phase: "validate", cause });
    return Response.json(
      { ok: false, system, status: "error", error: failure },
      { status: 500, headers: NO_STORE },
    );
  }
};
