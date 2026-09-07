/**
 * 公開 API `/v1` が必要とする環境変数。
 *
 * **収集用の `readCollectorEnv` を使い回さない。** あちらは `ODPT_ACCESS_TOKEN` と
 * `CONTACT_EMAIL` も必須にしており、読み取り専用の `/v1` がそれらの設定漏れで
 * 落ちる理由は無い。必要なものだけを要求する。
 *
 * 検証に失敗したときのメッセージには**変数名だけ**を載せる（CLAUDE.md §5）。
 */
import { z } from "zod";

const apiEnvSchema = z.object({
  SUPABASE_URL: z.url(),
  /** サービスロールキー。`/v1` は `v1_` ビューしか読まない（migration 0018、pgTAP 0010）。 */
  SUPABASE_SECRET_KEY: z.string().min(1),
});

export type ApiEnv = z.infer<typeof apiEnvSchema>;

export const readApiEnv = (source: Record<string, string | undefined>): ApiEnv => {
  const parsed = apiEnvSchema.safeParse(source);
  if (parsed.success) {
    return parsed.data;
  }
  const names = parsed.error.issues.map((issue) => issue.path.join(".")).join(", ");
  throw new Error(`必須の環境変数が未設定または不正です: ${names}`);
};
