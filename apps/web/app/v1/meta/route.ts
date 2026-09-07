/**
 * GET /v1/meta — データの鮮度・モデル版・クレジットを返す。
 * iOS / Web / 管理画面が同じ値を参照する（開発プラン §8.3）。
 *
 * **DB が落ちても 200 を返す。** この経路の役目は「いまデータがどういう状態か」を
 * 伝えることなので、伝えられないときは「分からない（＝古い）」と返す。500 にすると
 * クライアントは鮮度も表示条件も判断できなくなる。
 */
import { ATTRIBUTION_HEADER_VALUE, V1_CACHE_CONTROL } from "@bikechance/shared";
import { buildMeta } from "@/lib/api/meta";
import { createSupabaseReadPort, type FeedRow } from "@/lib/api/read-port";
import { readApiEnv } from "@/lib/api/env";
import { createServiceClient } from "@/lib/jobs/supabase";

/** `V1_MAX_DURATION_S` との一致は route.test.ts で検証する（リテラルでなければならない）。 */
export const maxDuration = 10;

/** 毎回 DB を見る。ビルド時の鮮度が焼き付くと「更新されない現在値」になる。 */
export const dynamic = "force-dynamic";

const HEADERS = {
  "Cache-Control": V1_CACHE_CONTROL,
  "X-Data-Attribution": ATTRIBUTION_HEADER_VALUE,
} as const;

/** 取れなければ null。呼び出し側が「分からない」として扱う。 */
const readFeeds = async (): Promise<readonly FeedRow[] | null> => {
  try {
    const env = readApiEnv(process.env);
    const client = createServiceClient({
      url: env.SUPABASE_URL,
      secret_key: env.SUPABASE_SECRET_KEY,
    });
    return await createSupabaseReadPort(client).listFeeds();
  } catch {
    return null;
  }
};

export const GET = async (): Promise<Response> => {
  const feeds = await readFeeds();
  const meta = buildMeta({ feeds, now: new Date(), contact_email: process.env.CONTACT_EMAIL });
  return Response.json(meta, { headers: HEADERS });
};
