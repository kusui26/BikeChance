/**
 * GET /v1/trip-check — 行程が成立する確率（W4 プラン §6.7、開発プラン §8.3）。
 *
 * 手順そのものは `lib/api/trip-check.ts` にある。ここは依存の組み立てと、
 * 状態コード・ヘッダの割り当てだけを行う（`/v1/stations` と同じ形）。
 *
 * **`/v1/stations` との違いは「時刻が 2 つあること」**である。出発ポートでは
 * `depart_in_min` の時点で借りられるか、到着ポートでは `arrive_in_min`
 * （＝ `depart_in_min ＋ ride_min`）の時点で返せるかを見る。
 *
 * **`p_trip` は独立仮定の注記と同じ欄に入れて返す**（W4-24）。数だけ取り出して
 * 注記を落とせないようにするためで、両端の予測がそろわなければ `trip` ごと null になる
 * （契約 10）。
 *
 * 認証は掛けない。CDN にキャッシュさせるため Authorization ヘッダを要求しない設計で
 * （CLAUDE.md §5）、保護は Vercel WAF のレート制限で行う。
 */
import {
  ATTRIBUTION_HEADER_VALUE,
  V1_CACHE_CONTROL,
  type TripCheckResponse,
} from "@bikechance/shared";
import { problemResponse, toProblem } from "@/lib/api/problem";
import { createSupabaseReadPort } from "@/lib/api/read-port";
import { queryTrip } from "@/lib/api/trip-check";
import { readApiEnv } from "@/lib/api/env";
import { createServiceClient } from "@/lib/jobs/supabase";

/**
 * `maxDuration` は**静的なリテラルでなければならない**（定数を import すると Next が
 * 静的解析できずビルドが落ちる）。`V1_MAX_DURATION_S` との一致は route.test.ts で検証する。
 */
export const maxDuration = 10;

/**
 * 毎回 DB を見る。Next の既定に任せるとビルド時の値が焼き付く可能性があり、
 * 「更新されない現在値」という最悪の壊れ方をする。
 */
export const dynamic = "force-dynamic";

const HEADERS = {
  "Cache-Control": V1_CACHE_CONTROL,
  "X-Data-Attribution": ATTRIBUTION_HEADER_VALUE,
} as const;

const okResponse = (response: TripCheckResponse): Response =>
  Response.json(response, { headers: HEADERS });

/** 入力の検証が済んでから呼ばれる。設定が足りなければここで例外になり 503 に写る。 */
const makePort = () => {
  const env = readApiEnv(process.env);
  return createSupabaseReadPort(
    createServiceClient({ url: env.SUPABASE_URL, secret_key: env.SUPABASE_SECRET_KEY }),
  );
};

export const GET = async (request: Request): Promise<Response> => {
  const outcome = await queryTrip({
    makePort,
    search: new URL(request.url).searchParams,
    now: new Date(),
  });
  return outcome.ok
    ? okResponse(outcome.response)
    : problemResponse(toProblem(outcome.failure), HEADERS);
};
