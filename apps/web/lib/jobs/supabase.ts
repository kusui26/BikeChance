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

export const createServiceClient = (params: {
  readonly url: string;
  readonly secret_key: string;
}): SupabaseClient =>
  createClient(params.url, params.secret_key, {
    auth: {
      // サーバーではセッションを持たない。トークンの更新も行わない。
      persistSession: false,
      autoRefreshToken: false,
    },
  });
