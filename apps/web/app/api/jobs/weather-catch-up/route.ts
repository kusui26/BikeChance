/**
 * GET /api/jobs/weather-catch-up — その時間の予報を、47 分に 1 度だけ取り直す（W12 プランの
 * W12-04・§4.3、W11 プランの所見 247）。
 *
 * **定時の回（17 分の `/api/jobs/archive-weather`）が 1 つも保存できなかった時間だけ**、7 分割
 * すべてを取る。保存できていれば何もせずに 200 を返す（`job_runs` には `skipped` を残す）。
 * 手順と、なぜ「1 つも保存できなかったときだけ」なのかは `lib/jobs/archive-weather.ts` の
 * `catchUpWeather` にある。**道を分けた**のは、回の種類を時刻から推し量らないため（手で呼んでも、
 * 遅れて呼ばれても、取り直しの回として動く）。
 *
 * ステータスコードの方針は 17 分のルートと同じ：
 *   200 何もしなかった・取れた   401 CRON_SECRET 不一致   500 取れなかった・確かめられなかった
 * Cron はリダイレクトを追わないので 3xx は返さない（CLAUDE.md §3）。
 */
import { catchUpWeather, type CatchUpSummary } from "@/lib/jobs/archive-weather";
import { isAuthorizedCronRequest } from "@/lib/jobs/auth";
import { isJobError, toJobFailure } from "@/lib/jobs/errors";
import { weatherJobParams } from "@/lib/jobs/weather-job";

/**
 * `maxDuration` は**静的なリテラルでなければならない**（17 分のルートと同じ理由）。取るときは
 * 17 分の回と同じ手順・同じ最悪の所要なので、同じ上限（`ARCHIVE_WEATHER_MAX_DURATION_S`）に
 * する。一致は route.test.ts で検証する。
 */
export const maxDuration = 300;

const NO_STORE = { "Cache-Control": "no-store" } as const;

const problem = (status: number, title: string, detail: string): Response =>
  Response.json({ ok: false, title, detail }, { status, headers: NO_STORE });

const summaryResponse = (summary: CatchUpSummary): Response =>
  Response.json(summary, { status: summary.ok ? 200 : 500, headers: NO_STORE });

export const GET = async (request: Request): Promise<Response> => {
  // 1. 認証。DB にもログにも何も書かずに弾く
  if (!isAuthorizedCronRequest(request.headers.get("authorization"), process.env.CRON_SECRET)) {
    return problem(401, "unauthorized", "CRON_SECRET が一致しません。");
  }

  // 2. 環境変数 → その時間の発行が在るか → （無ければ）17 分の回と同じ手順で取る → 記録
  try {
    return summaryResponse(await catchUpWeather(weatherJobParams(process.env, new Date())));
  } catch (cause) {
    const failure = isJobError(cause) ? cause.failure : toJobFailure({ phase: "validate", cause });
    return Response.json({ ok: false, error: failure }, { status: 500, headers: NO_STORE });
  }
};
