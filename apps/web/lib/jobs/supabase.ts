/**
 * サービスロールの Supabase クライアント。**サーバー専用**。
 *
 * 環境変数に `NEXT_PUBLIC_` を付けていないため、この値がクライアントバンドルに
 * 入ることはない。呼び出してよいのは Route Handler とローカルスクリプトだけ。
 *
 * W1-1 の決定どおり、収集器は PostgREST 経由（supabase-js）で DB と Storage を扱う。
 * 直接接続（psycopg）は W2 の Python サービスとスクリプトだけが使う。
 */
import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/**
 * 1 要求ごとに時間切れを付けた `fetch`（W7-03、W6 プランの所見 220 の ②）。呼び出し側が
 * 渡した中断の合図とも合わせる（どちらかが来たら止める）。
 *
 * supabase-js の Storage の書き込み（`upload`）は中断の合図を受け取らないので、
 * **クライアントの `fetch` で付ける**——DB の RPC と Storage の両方に 1 か所で効く。
 */
export const fetchWithTimeout =
  (timeout_ms: number, base: typeof fetch = fetch): typeof fetch =>
  (input, init) => {
    const timeout = AbortSignal.timeout(timeout_ms);
    const signal = init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
    return base(input, { ...init, signal });
  };

export const createServiceClient = (params: {
  readonly url: string;
  readonly secret_key: string;
  /**
   * 1 要求の時間切れ（ミリ秒）。**渡したジョブのクライアントだけに効く**——いまは天気の
   * アーカイブだけが渡す。収集・`/v1` は渡さず、いままでどおり（W7-03）。
   */
  readonly request_timeout_ms?: number;
}): SupabaseClient =>
  createClient(params.url, params.secret_key, {
    auth: {
      // サーバーではセッションを持たない。トークンの更新も行わない。
      persistSession: false,
      autoRefreshToken: false,
    },
    ...(params.request_timeout_ms === undefined
      ? {}
      : { global: { fetch: fetchWithTimeout(params.request_timeout_ms) } }),
  });
