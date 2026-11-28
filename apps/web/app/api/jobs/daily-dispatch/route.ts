/**
 * GET /api/jobs/daily-dispatch — GitHub Actions の日次の枠が来ていなければ、`workflow_dispatch` で
 * 起こす（開発プランの D-50、W13 プランの W13-07・§4.6）。10:15・10:35 JST の Vercel Cron。
 *
 * 何を起こすか（特徴量 → 日次評価、1 回に 1 つ、同じ日に同じものを 2 度起こさない）は
 * `lib/jobs/daily-dispatch.ts` にある。
 *
 * ステータスコードの方針は天気のルートと同じ：
 *   200 起こした・何もしなかった   401 CRON_SECRET 不一致   500 起こせなかった・確かめられなかった
 * Cron はリダイレクトを追わないので 3xx は返さない（CLAUDE.md §3）。
 */
import { isAuthorizedCronRequest } from "@/lib/jobs/auth";
import { dispatchDaily, type DispatchSummary } from "@/lib/jobs/daily-dispatch";
import { dailyDispatchParams } from "@/lib/jobs/daily-dispatch-job";
import { isJobError, toJobFailure } from "@/lib/jobs/errors";

/**
 * `maxDuration` は**静的なリテラルでなければならない**（Next.js がビルド時に読む）。
 * 値は `DAILY_DISPATCH_MAX_DURATION_S` と同じで、一致は route.test.ts で検証する。
 */
export const maxDuration = 60;

const NO_STORE = { "Cache-Control": "no-store" } as const;

const problem = (status: number, title: string, detail: string): Response =>
  Response.json({ ok: false, title, detail }, { status, headers: NO_STORE });

const summaryResponse = (summary: DispatchSummary): Response =>
  Response.json(summary, { status: summary.ok ? 200 : 500, headers: NO_STORE });

export const GET = async (request: Request): Promise<Response> => {
  // 1. 認証。DB にもログにも何も書かずに弾く
  if (!isAuthorizedCronRequest(request.headers.get("authorization"), process.env.CRON_SECRET)) {
    return problem(401, "unauthorized", "CRON_SECRET が一致しません。");
  }

  // 2. 環境変数 → 昨日の枠を読む → （要れば）起こす → 記録
  try {
    return summaryResponse(await dispatchDaily(dailyDispatchParams(process.env, new Date())));
  } catch (cause) {
    const failure = isJobError(cause) ? cause.failure : toJobFailure({ phase: "validate", cause });
    return Response.json({ ok: false, error: failure }, { status: 500, headers: NO_STORE });
  }
};
