/** 公開 API `/v1` のスキーマ。iOS / Web / テストがこの型を共有する。 */
import { z } from "zod";
import { SYSTEM_IDS } from "./constants";

export const systemIdSchema = z.enum(SYSTEM_IDS);

export const attributionSchema = z.object({
  system_id: systemIdSchema,
  provider: z.string(),
  dataset: z.string(),
  license: z.string(),
  license_url: z.url(),
});

export const feedStatusSchema = z.object({
  system_id: systemIdSchema,
  display_name: z.string(),
  /** フィードの last_updated（ISO 8601）。未収集なら null。 */
  data_updated_at: z.iso.datetime().nullable(),
  expected_cadence_s: z.number().int().positive(),
  /** これを超えて観測が無ければ `stale`。監視（migration 0013）と同じ式で出す。 */
  stale_after_s: z.number().int().positive(),
  /** 観測が途切れている。値は返すが「現在値」として扱わない（CLAUDE.md §2 の 8）。 */
  stale: z.boolean(),
  /**
   * `capacity` が固定のラック数ではなく `bikes + docks` の動的値であること。
   * ドコモが true（開発プラン §3.6）。空き枠の解釈がシステムで違うことを利用者に伝える。
   */
  capacity_is_dynamic: z.boolean(),
});

export const metaResponseSchema = z.object({
  api_version: z.literal("v1"),
  generated_at: z.iso.datetime(),
  /**
   * **いずれかのフィードの観測が途切れている**。クライアントは値に観測時刻を添えて出す。
   * 予測の有無は `model_version` を見る（W2 時点では常に null）。
   */
  stale: z.boolean(),
  model_version: z.string().nullable(),
  feeds: z.array(feedStatusSchema),
  attribution: z.array(attributionSchema),
  notice: z.string(),
  disclaimer: z.string(),
});

/**
 * `/v1/stations` の 1 要素。
 *
 * **NULL の意味を型で表す。**
 *   * `name` / `capacity` … 属性をまだ取れていない（新しいポートは最大 1 日。開発プラン §8.3）
 *   * `bikes` / `docks` / `is_*` … 一度も観測されていない。**0 や false と区別する**
 *   * `observed_at` … 最新のフィードにこのポートが現れなかった（`is_present` が false）
 */
export const stationCurrentSchema = z.object({
  system_id: systemIdSchema,
  station_id: z.string().min(1),
  name: z.string().nullable(),
  lat: z.number(),
  lon: z.number(),
  /** ドコモは動的値。解釈は `feeds[].capacity_is_dynamic` を見て決める。 */
  capacity: z.number().int().nonnegative().nullable(),
  bikes: z.number().int().nonnegative().nullable(),
  docks: z.number().int().nonnegative().nullable(),
  is_installed: z.boolean().nullable(),
  is_renting: z.boolean().nullable(),
  is_returning: z.boolean().nullable(),
  /** 最新のフィードにこのポートが現れたか。false の値は「いつのものか分からない」。 */
  is_present: z.boolean(),
  /** この値が現在値と言える時刻（＝フィードの last_updated）。不在なら null。 */
  observed_at: z.iso.datetime().nullable(),
  /** 台数・枠・フラグのいずれかが最後に「変わった」時刻。最後に観測した時刻ではない。 */
  last_changed_at: z.iso.datetime(),
});

export const bboxSchema = z.object({
  west: z.number(),
  south: z.number(),
  east: z.number(),
  north: z.number(),
});

export const stationsResponseSchema = z.object({
  api_version: z.literal("v1"),
  generated_at: z.iso.datetime(),
  /**
   * 実際に検索した矩形。要求された bbox を格子に**外側へ**丸めたもの。
   * 応答には要求範囲の外のポートも混じるので、必要なら利用者側で絞り込む。
   */
  bbox: bboxSchema,
  count: z.number().int().nonnegative(),
  /** いずれかのフィードの観測が途切れている。 */
  stale: z.boolean(),
  feeds: z.array(feedStatusSchema),
  stations: z.array(stationCurrentSchema),
  /** CC BY 4.0 の表示に要る。応答だけで表示を完結できるようにする（開発プラン §3.7）。 */
  attribution: z.array(attributionSchema),
});

/**
 * エラー応答（RFC 9457 Problem Details）。開発プラン §8.3。
 *
 * `type` は相対 URI にする。ドメインを決め打ちにせず、`about:blank` のように
 * 種類を潰すこともしない。`code` は機械が分岐するための拡張メンバ。
 */
export const problemSchema = z.object({
  type: z.string().min(1),
  title: z.string().min(1),
  status: z.number().int().min(400).max(599),
  detail: z.string(),
  code: z.string().min(1),
});

export type MetaResponse = z.infer<typeof metaResponseSchema>;
export type Attribution = z.infer<typeof attributionSchema>;
export type FeedStatus = z.infer<typeof feedStatusSchema>;
export type StationCurrent = z.infer<typeof stationCurrentSchema>;
export type StationsResponse = z.infer<typeof stationsResponseSchema>;
export type Problem = z.infer<typeof problemSchema>;
