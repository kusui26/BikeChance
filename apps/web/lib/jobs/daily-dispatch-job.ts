/**
 * 日次の枠を起こすルート（`/api/jobs/daily-dispatch`。開発プランの D-50）の依存を組み立てる。
 */
import { GITHUB_API_TIMEOUT_MS, WEATHER_DB_TIMEOUT_MS } from "@bikechance/shared";
import type { DispatchParams } from "./daily-dispatch";
import { createSupabaseDispatchPort } from "./dispatch-port";
import { readDispatchEnv } from "./env";
import { createGitHubPort } from "./github-dispatch";
import { createServiceClient } from "./supabase";

/** 起こすワークフローの在りか（`owner/name`）。**このリポジトリのほかは起こさない**（D-50）。 */
export const DISPATCH_REPOSITORY = "kusui26/BikeChance";

/** 環境変数から依存を組み立てる。**足りなければ投げる**（ルートが 500 を返し、何も起こさない）。 */
export const dailyDispatchParams = (
  source: Record<string, string | undefined>,
  now: Date,
): DispatchParams => {
  const env = readDispatchEnv(source);
  // DB の 1 要求にも時間切れ（天気のジョブと同じ値。W7-03）
  const client = createServiceClient({
    url: env.SUPABASE_URL,
    secret_key: env.SUPABASE_SECRET_KEY,
    request_timeout_ms: WEATHER_DB_TIMEOUT_MS,
  });
  const token = env.GITHUB_DISPATCH_TOKEN;
  return {
    db: createSupabaseDispatchPort(client),
    github: createGitHubPort({
      token,
      repository: DISPATCH_REPOSITORY,
      timeout_ms: GITHUB_API_TIMEOUT_MS,
    }),
    now,
    secrets: [token],
  };
};
