/**
 * GBFS v2.3 フィードの Zod スキーマ（W1 プラン §6.4）。
 *
 * **未知のフィールドは落とさない**（`z.looseObject`）。GBFS 3.0 への移行や事業者独自の
 * 拡張が来ても、生 JSON と同じ情報を保ったまま扱えるようにする（開発プラン §3.6）。
 *
 * **検証は例外ではなく結果で返す**。PR D の収集経路では、検証に失敗しても生 JSON の保存は
 * 続けなければならない（生 JSON が一次ソースであり、後から何度でも再処理できる）。
 * 例外を投げる設計だと、呼び出し側が try/catch を書き忘れた瞬間にデータを失う。
 *
 * 2026-09-06 の実フィードで確認した型：
 *   HELLO   station_status  : is_* は bool、last_reported は全件あり、vehicle_* 配列を持つ
 *   ドコモ  station_status  : vehicle_* 配列なし、last_reported は全件 last_updated と同値
 *   HELLO   station_information: capacity なし、**vehicle_capacity が文字列**（非標準）
 *   ドコモ  station_information: capacity は整数（0 が 230 件、最大 9999）、region_id は文字列
 */
import { z } from "zod";

/** Postgres の smallint の上限。配列に入らない値は取り込み前に弾く（§11.1）。 */
export const SMALLINT_MAX = 32767;

/**
 * GBFS の `last_updated` は POSIX 秒。ミリ秒や時計ずれを弾くための妥当範囲。
 * 秒とミリ秒を取り違えると、Storage のパスが西暦 57000 年になる。
 */
export const MIN_PLAUSIBLE_EPOCH_S = 1_577_836_800; // 2020-01-01T00:00:00Z
export const MAX_PLAUSIBLE_EPOCH_S = 4_102_444_800; // 2100-01-01T00:00:00Z

const epochSecondsSchema = z.int().min(MIN_PLAUSIBLE_EPOCH_S).max(MAX_PLAUSIBLE_EPOCH_S);

/**
 * 台数・返却枠。
 *
 * **GBFS の仕様は非負だが、実データは違う。** HELLO は定員超過のポートで
 * `num_docks_available` に **-1** を返す（2026-09-06 の実測で 17 件）。
 * 容量 3 のポートに 4 台停まっていれば `capacity − bikes = -1` になる、という素直な帰結で、
 * 開発プラン §3.5 が観測していた「定員超過駐輪」と同じ現象である。
 *
 * ここで弾くと HELLO のフィードが丸ごと取り込めなくなるので、**検証では受け取り、
 * 0 への丸めは正規化で行う**（`normalize.ts`）。`-1` は「登録済みだが今回現れなかった」
 * を表す予約値なので、そのまま保存すると意味が衝突する（§11.1）。
 *
 * smallint に収まらない値だけを弾く。それは取り込み時に必ず失敗するため、
 * 生 JSON を残したうえで気づけた方がよい。
 */
const stationCountSchema = z.int().min(-SMALLINT_MAX).max(SMALLINT_MAX);

export const stationStatusEntrySchema = z.looseObject({
  station_id: z.string().min(1),
  num_bikes_available: stationCountSchema,
  num_docks_available: stationCountSchema,
  is_installed: z.boolean(),
  is_renting: z.boolean(),
  is_returning: z.boolean(),
  /** ポートが事業者サーバーへ最後に報告した時刻。ドコモは全件 `last_updated` と同値。 */
  last_reported: epochSecondsSchema.optional(),
});

export const stationInformationEntrySchema = z.looseObject({
  station_id: z.string().min(1),
  name: z.string(),
  lat: z.number(),
  lon: z.number(),
  /** ドコモのみ。固定ラック数ではなく `bikes + docks` の動的値（開発プラン §3.6）。 */
  capacity: z.int().min(0).optional(),
  /**
   * HELLO のみ。GBFS 2.3 ではバーチャルステーション用のオブジェクト型なので**非標準**。
   * 実データは数字の文字列。落とさずに受け取り、数値化は利用側で行う。
   */
  vehicle_capacity: z.union([z.string(), z.number()]).optional(),
});

const feedEnvelope = <T extends z.ZodTypeAny>(entry: T) =>
  z.looseObject({
    last_updated: epochSecondsSchema,
    ttl: z.int().min(0).optional(),
    version: z.string().optional(),
    data: z.looseObject({ stations: z.array(entry) }),
  });

export const stationStatusFeedSchema = feedEnvelope(stationStatusEntrySchema);
export const stationInformationFeedSchema = feedEnvelope(stationInformationEntrySchema);

export type StationStatusEntry = z.infer<typeof stationStatusEntrySchema>;
export type StationInformationEntry = z.infer<typeof stationInformationEntrySchema>;
export type StationStatusFeed = z.infer<typeof stationStatusFeedSchema>;
export type StationInformationFeed = z.infer<typeof stationInformationFeedSchema>;

/** 検証の結果。**例外は投げない**（このファイルの冒頭を参照）。 */
export type ParseOutcome<T> =
  | { readonly ok: true; readonly feed: T }
  | { readonly ok: false; readonly issues: readonly string[] };

/** Zod の問題を「どこが・なぜ」だけの短い行にする。値は載せない（フィードの中身をログに流さない）。 */
const describeIssues = (error: z.ZodError): readonly string[] =>
  error.issues.slice(0, 10).map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.code}`);

const toOutcome = <T>(result: z.ZodSafeParseResult<T>): ParseOutcome<T> =>
  result.success
    ? { ok: true, feed: result.data }
    : { ok: false, issues: describeIssues(result.error) };

export const parseStationStatusFeed = (input: unknown): ParseOutcome<StationStatusFeed> =>
  toOutcome(stationStatusFeedSchema.safeParse(input));

export const parseStationInformationFeed = (input: unknown): ParseOutcome<StationInformationFeed> =>
  toOutcome(stationInformationFeedSchema.safeParse(input));
