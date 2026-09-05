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

export const feedFreshnessSchema = z.object({
  system_id: systemIdSchema,
  display_name: z.string(),
  /** フィードの last_updated（ISO 8601）。未収集なら null。 */
  data_updated_at: z.iso.datetime().nullable(),
  expected_cadence_s: z.number().int().positive(),
});

export const metaResponseSchema = z.object({
  api_version: z.literal("v1"),
  generated_at: z.iso.datetime(),
  /** 予測が古い、または未生成の場合に true。クライアントは現在値のみ表示する。 */
  stale: z.boolean(),
  model_version: z.string().nullable(),
  feeds: z.array(feedFreshnessSchema),
  attribution: z.array(attributionSchema),
  notice: z.string(),
  disclaimer: z.string(),
});

export type MetaResponse = z.infer<typeof metaResponseSchema>;
export type Attribution = z.infer<typeof attributionSchema>;
export type FeedFreshness = z.infer<typeof feedFreshnessSchema>;
