/**
 * GET /v1/stations — 矩形の中のポートの現在値（W2 プラン §5.7、開発プラン §8.3）。
 *
 * 手順そのものは `lib/api/stations.ts` にある。ここは依存の組み立てと、
 * 状態コード・ヘッダの割り当てだけを行う（`/api/jobs/*` と同じ形）。
 *
 * **予測は返さない。** `p_bike` / `p_dock` は W4 で足す。ここで返すのは実測値だけで、
 * 値には必ず観測時刻が付く（CLAUDE.md §2 の 7）。
 *
 * 認証は掛けない。CDN にキャッシュさせるため Authorization ヘッダを要求しない設計で
 * （CLAUDE.md §5）、保護は Vercel WAF のレート制限で行う。
 */
import {
  ATTRIBUTION_HEADER_VALUE,
  V1_CACHE_CONTROL,
  type StationsResponse,
} from "@bikechance/shared";
import { problemResponse, toProblem } from "@/lib/api/problem";
import { createSupabaseReadPort } from "@/lib/api/read-port";
import { queryStations } from "@/lib/api/stations";
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

const okResponse = (response: StationsResponse): Response =>
  Response.json(response, { headers: HEADERS });

/** 入力の検証が済んでから呼ばれる。設定が足りなければここで例外になり 503 に写る。 */
const makePort = () => {
  const env = readApiEnv(process.env);
  return createSupabaseReadPort(
    createServiceClient({ url: env.SUPABASE_URL, secret_key: env.SUPABASE_SECRET_KEY }),
  );
};

export const GET = async (request: Request): Promise<Response> => {
  const outcome = await queryStations({
    makePort,
    search: new URL(request.url).searchParams,
    now: new Date(),
  });
  return outcome.ok
    ? okResponse(outcome.response)
    : problemResponse(toProblem(outcome.failure), HEADERS);
};
