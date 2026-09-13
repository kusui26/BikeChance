/**
 * GET /v1/stations/{system}/{station_id} — ポート 1 件の詳細（開発プラン §8.3、W5 の PR F）。
 *
 * 手順そのものは `lib/api/station-detail.ts` にある。ここは依存の組み立てと、
 * 状態コード・ヘッダの割り当てだけを行う（`/v1/stations` と同じ形）。
 *
 * 返すのは **現在値 ＋ 予測の生の 10 点 ＋ 直近 24 時間の実績**である。**曲線は補間
 * しない**（W5-13）——地図が返す「その到着時刻の 1 点」と正が同じで、同じ確率を
 * 2 つの形で配らないための切り分けになっている（契約 4・16）。
 *
 * **`at` を受けない。** 曲線を返すので到着時刻が要らず、受けると同じポートの URL が
 * 5 分ごとに割れて CDN が効かない。
 *
 * 認証は掛けない。CDN にキャッシュさせるため Authorization ヘッダを要求しない設計で
 * （CLAUDE.md §5）、保護は Vercel WAF のレート制限で行う。
 */
import {
  ATTRIBUTION_HEADER_VALUE,
  V1_CACHE_CONTROL,
  type StationDetailResponse,
} from "@bikechance/shared";
import { problemResponse, toProblem } from "@/lib/api/problem";
import { createSupabaseReadPort } from "@/lib/api/read-port";
import { queryStationDetail } from "@/lib/api/station-detail";
import { readApiEnv } from "@/lib/api/env";
import { createServiceClient } from "@/lib/jobs/supabase";

/**
 * `maxDuration` は**静的なリテラルでなければならない**（定数を import すると Next が
 * 静的解析できずビルドが落ちる）。`V1_MAX_DURATION_S` との一致は route.test.ts で検証する。
 */
export const maxDuration = 10;

/** 毎回 DB を見る。ビルド時の値が焼き付くと「更新されない現在値」になる。 */
export const dynamic = "force-dynamic";

const HEADERS = {
  "Cache-Control": V1_CACHE_CONTROL,
  "X-Data-Attribution": ATTRIBUTION_HEADER_VALUE,
} as const;

const okResponse = (response: StationDetailResponse): Response =>
  Response.json(response, { headers: HEADERS });

/** 入力の検証が済んでから呼ばれる。設定が足りなければここで例外になり 503 に写る。 */
const makePort = () => {
  const env = readApiEnv(process.env);
  return createSupabaseReadPort(
    createServiceClient({ url: env.SUPABASE_URL, secret_key: env.SUPABASE_SECRET_KEY }),
  );
};

/**
 * 経路の引数は **Promise で届く**（Next.js 16）。
 *
 * 型を自分で書いているのは、生成される `RouteContext` が `next typegen` の後にしか
 * 存在しないためで、**型検査の順序に依存させない**。
 */
type Context = {
  readonly params: Promise<{ readonly system: string; readonly station_id: string }>;
};

export const GET = async (_request: Request, context: Context): Promise<Response> => {
  const { system, station_id } = await context.params;
  const outcome = await queryStationDetail({
    makePort,
    system,
    station_id: decodeURIComponent(station_id),
    now: new Date(),
  });
  return outcome.ok
    ? okResponse(outcome.response)
    : problemResponse(toProblem(outcome.failure), HEADERS);
};
