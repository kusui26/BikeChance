/**
 * 収集ジョブが必要とする環境変数（W1 プラン §11.7）。
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

export const readCollectorEnv = (source: Record<string, string | undefined>): CollectorEnv => {
  const parsed = collectorEnvSchema.safeParse(source);
  if (parsed.success) {
    return parsed.data;
  }
  const names = parsed.error.issues.map((issue) => issue.path.join(".")).join(", ");
  throw new Error(`必須の環境変数が未設定または不正です: ${names}`);
};
