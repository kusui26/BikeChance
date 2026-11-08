/**
 * 問い合わせ先（W10-07）。**ODPT の通知文（`/v1/meta` の `notice`）と同じ `CONTACT_EMAIL`** を使う。
 *
 * 頁は静的に作るので、読むのは**ビルドのとき**である（本番のビルドには Vercel の環境変数が入る）。
 * CI のビルドには入っていないので、そのときは `null` を返し、頁は「準備中」と出す——
 * `/v1/meta` の控え（`contact@example.com`）は**公開の頁には出さない**（実在しない宛先を案内しない）。
 */
import { z } from "zod";

const contactEmailSchema = z.email();

export const readContactEmail = (source: Record<string, string | undefined>): string | null => {
  const parsed = contactEmailSchema.safeParse(source["CONTACT_EMAIL"]);
  return parsed.success ? parsed.data : null;
};

/** 問い合わせ先がビルドに無かったときに出す文。 */
export const CONTACT_PENDING_TEXT = "お問い合わせ先は準備中です。";
