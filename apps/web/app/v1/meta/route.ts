/**
 * GET /v1/meta — データの鮮度・モデル版・クレジットを返す。
 * iOS / Web / 管理画面が同じ値を参照する（開発プラン §8.3）。
 * 収集と推論の接続は後続の PR で行うため、現時点では鮮度を null（stale）で返す。
 */
import {
  ATTRIBUTIONS,
  ATTRIBUTION_HEADER_VALUE,
  FORECAST_DISCLAIMER,
  SYSTEMS,
  SYSTEM_IDS,
  buildOdptNotice,
  metaResponseSchema,
  type MetaResponse,
} from "@bikechance/shared";

const CACHE_CONTROL = "public, s-maxage=60, stale-while-revalidate=120";
const FALLBACK_CONTACT_EMAIL = "contact@example.com";

const buildMeta = (now: Date): MetaResponse =>
  metaResponseSchema.parse({
    api_version: "v1",
    generated_at: now.toISOString(),
    stale: true,
    model_version: null,
    feeds: SYSTEM_IDS.map((systemId) => ({
      system_id: systemId,
      display_name: SYSTEMS[systemId].display_name,
      data_updated_at: null,
      expected_cadence_s: SYSTEMS[systemId].expected_cadence_s,
    })),
    attribution: ATTRIBUTIONS,
    notice: buildOdptNotice(process.env.CONTACT_EMAIL ?? FALLBACK_CONTACT_EMAIL),
    disclaimer: FORECAST_DISCLAIMER,
  });

export const GET = (): Response =>
  Response.json(buildMeta(new Date()), {
    headers: {
      "Cache-Control": CACHE_CONTROL,
      "X-Data-Attribution": ATTRIBUTION_HEADER_VALUE,
    },
  });
