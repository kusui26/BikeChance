/**
 * ジョブが必要とする環境変数（W1 プラン §11.7。日次の枠を起こすジョブは D-50）。
 *
 * 起動時ではなくハンドラの中で読む。モジュール読み込み時に検証すると、
 * ビルドやテストが環境変数の有無に左右されるため。
 *
 * 検証に失敗したときのメッセージには**変数名だけ**を載せ、値は決して載せない。
 */
import { z } from "zod";

const collectorEnvSchema = z.object({
  /** ODPT の認証付き取得に使う。クエリ acl:consumerKey に載る（開発プラン D-04）。 */
  ODPT_ACCESS_TOKEN: z.string().min(1),
  SUPABASE_URL: z.url(),
  /** サービスロールキー。サーバーからの書き込み専用。 */
  SUPABASE_SECRET_KEY: z.string().min(1),
  /** ODPT の通知文と User-Agent に載せる連絡先（§5.1 の 18）。 */
  CONTACT_EMAIL: z.string().min(1),
});

export type CollectorEnv = z.infer<typeof collectorEnvSchema>;

/**
 * 日次の枠を起こすジョブ（`/api/jobs/daily-dispatch`。開発プランの D-50）の環境変数。
 * **ODPT のトークンと連絡先は要らない**（GitHub の API と DB だけを使う）。
 */
const dispatchEnvSchema = z.object({
  SUPABASE_URL: z.url(),
  SUPABASE_SECRET_KEY: z.string().min(1),
  /**
   * GitHub の fine-grained トークン。**このリポジトリだけ・Actions の読み書きとメタデータの読みだけ**
   * （D-50）。`Authorization` の見出しにだけ載せ、記録・例外・応答に出さない。
   */
  GITHUB_DISPATCH_TOKEN: z.string().min(1),
});

export type DispatchEnv = z.infer<typeof dispatchEnvSchema>;

/** 検証に失敗したら**変数名だけ**を載せて投げる（値は載せない）。 */
const parseEnv = <T>(schema: z.ZodType<T>, source: Record<string, string | undefined>): T => {
  const parsed = schema.safeParse(source);
  if (parsed.success) {
    return parsed.data;
  }
  const names = parsed.error.issues.map((issue) => issue.path.join(".")).join(", ");
  throw new Error(`必須の環境変数が未設定または不正です: ${names}`);
};

export const readCollectorEnv = (source: Record<string, string | undefined>): CollectorEnv =>
  parseEnv(collectorEnvSchema, source);

export const readDispatchEnv = (source: Record<string, string | undefined>): DispatchEnv =>
  parseEnv(dispatchEnvSchema, source);
