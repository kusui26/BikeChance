/**
 * ログ・エラー本文に出る文字列から ODPT のアクセストークンを伏字にする（W1 プラン W1-21 の 3）。
 *
 * ODPT はキーをクエリ `acl:consumerKey` でしか受け付けないため（開発プラン D-04）、
 * 認証付きで取得する限りトークンは必ず URL に載る。通信経路では漏れない（HTTPS は
 * クエリを含むリクエスト行を暗号化する）が、自分のコードや基盤が URL を記録すると漏れる。
 *
 * 漏洩は本来 1（URL の組み立てを `odpt-fetch.ts` に閉じ込める）と 2（例外を詰め替える）で
 * 防ぐ。このモジュールはそれらをすり抜けた場合の最後の網であり、単独で頼るものではない。
 */

/** `acl:consumerKey=...` の値部分。区切り文字と引用符の手前まで。 */
const CONSUMER_KEY_PATTERN = /(acl:consumerKey=)[^&\s"'<>]+/gi;

const REDACTED = "***";

/**
 * 短すぎる秘密は伏字にしない。
 * 例えば 1 文字の値で置換すると文章が壊れ、かえって読めなくなる。
 * ODPT のトークンは十分に長いため実害はない。
 */
const MIN_SECRET_LENGTH = 8;

/** クエリパラメータの形をしたトークンを伏字にする。 */
export const redactConsumerKey = (text: string): string =>
  text.replace(CONSUMER_KEY_PATTERN, `$1${REDACTED}`);

/**
 * 既知の秘密そのものを伏字にする。
 * `acl:consumerKey=` の前置きが無い形（値だけがログに出た場合）を捕まえる。
 */
export const redactLiteral = (text: string, secret: string): string =>
  secret.length < MIN_SECRET_LENGTH ? text : text.split(secret).join(REDACTED);

/**
 * 記録する文字列は必ずこれを通す。
 * `secrets` には実際のトークン値を渡す（呼び出し側が知っている場合）。
 */
export const redact = (text: string, secrets: readonly string[] = []): string =>
  secrets.reduce((acc, secret) => redactLiteral(acc, secret), redactConsumerKey(text));
