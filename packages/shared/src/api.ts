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
/**
 * 1 ポートぶんの予測（W4 プラン §4 の W4-01・W4-02）。
 *
 * **要求に `at` か `in_min` が無ければ、この欄そのものが null になる。** 「いまの確率」は
 * 現在値そのものであって予測ではない。
 *
 * 指定があっても null になる場合が 3 つある：貸出も返却も止まっている／観測が古い／
 * 成果物にそのポートが無い。**理由は返さない。** 利用者にとっては「いまは出せない」で
 * 十分で、原因は `inference_log` と監視が持つ（内部の都合を API の契約に漏らさない）。
 *
 * **確率が指す時刻は `generated_at ＋ forecast_in_min`**（どちらも応答の中にある）。
 * 応答が CDN に留まっていた時間だけ、読んだ人の「いま」からは離れる（最大 3 分。
 * `s-maxage=60` ＋ `stale-while-revalidate=120`）。**「約 30 分後」ではなく到着の時刻で
 * 表示する**のが安全である（W4 プラン §12 の 114）。
 */
export const stationForecastSchema = z.object({
  /** 借りられる確率（0〜1）。元は 1/1000 刻み。 */
  p_bike: z.number().min(0).max(1),
  /** 返せる確率（0〜1）。 */
  p_dock: z.number().min(0).max(1),
  /** 0〜3。3 が最も確か（気候値が過半の水平で効いた）。 */
  confidence: z.number().int().min(0).max(3),
  /** **この予測が基づく観測の時刻。** 鮮度はこれで測る（`generated_at` ではない）。 */
  base_observed_at: z.iso.datetime(),
  /** この値を出したモデルの版。 */
  model_version: z.string().min(1),
});

export const stationCurrentSchema = z.object({
  system_id: systemIdSchema,
  station_id: z.string().min(1),
  name: z.string().nullable(),
  lat: z.number(),
  lon: z.number(),
  /**
   * **固定のラック数**。**容量が動的なシステム（`feeds[].capacity_is_dynamic`）では null**
   * になる（W4 の PR B′、migration 0035）。
   *
   * ドコモの公開値は日次同期の瞬間の `bikes + docks` が凍結されたもので、ラック数では
   * ない。渡すと「容量 5・借りられる 12」という矛盾が画面に出る（W4 プラン §12 の 115）。
   * **ポートの大きさは `capacity_est` が答える**（2026-09-13 に足した。W5-14）。
   */
  capacity: z.number().int().nonnegative().nullable(),
  /**
   * **実測からのポートの大きさ**（前日までの 7 日の `max(bikes + docks)`。W5-14、
   * migration 0045）。**`capacity` とは別の数**で、混ぜない——前者は「事業者が
   * 宣言したラック数」、こちらは「7 日のあいだに実際に並んだ最大」である。
   *
   * **どちらのシステムでも出る**のがこの欄の値打ちで、`capacity` が NULL になる
   * ドコモの 5,837 ポートにも大きさが付く（W4-09 の宿題）。
   *
   * NULL は「まだ分からない」：7 日のあいだ 1 度も観測できなかったポートと、
   * 参照スナップショットをまだ写していないポート。
   *
   * **0 は入る**（実測 37 件・0.18%）。「7 日のあいだ 1 台も 1 枠も並ばなかった」という
   * 観測であって、「分からない」ではない——`bikes = 0` と `bikes = null` を分けているのと
   * 同じ約束である（W5 プラン §12 の 152）。
   */
  capacity_est: z.number().int().nonnegative().nullable(),
  /**
   * `capacity_est` に寄与した日数（1〜7）。**足りないことを隠さない**ための組で、
   * 7 未満なら「まだその日数ぶんしか見ていない」と読む。`capacity_est` が NULL なら
   * こちらも NULL。
   */
  capacity_days: z.number().int().min(1).max(7).nullable(),
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
  /** 到着時刻の予測。**要求に `at` / `in_min` が無ければ null**（W4-02）。 */
  forecast: stationForecastSchema.nullable(),
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
  /**
   * 予測を出した到着時刻（**5 分に丸めた後**）。`at` / `in_min` が無ければ null。
   *
   * **丸めた後の値を返す**ので、`in_min=37` で要求すると 35 が返る。何分の予測を見て
   * いるのかを、利用者が要求の文字列ではなく応答から読めるようにする。
   *
   * **確率が指すのは `generated_at` からこの分数だけ先**である。`generated_at` は応答を
   * 作った時刻で、読んだ時刻ではない。
   */
  forecast_in_min: z.number().int().positive().nullable(),
  feeds: z.array(feedStatusSchema),
  stations: z.array(stationCurrentSchema),
  /** CC BY 4.0 の表示に要る。応答だけで表示を完結できるようにする（開発プラン §3.7）。 */
  attribution: z.array(attributionSchema),
});

/**
 * 行程が成立する確率（W4 プラン §6.7、W4-24）。
 *
 * **`trip` が非 null であることと、`from.forecast` と `to.forecast` が両方非 null で
 * あることは同値**である（契約 10）。片側でも欠けたら掛け算をしない——欠けた値を
 * 0 とみなしたり、片側だけで代用したりしない。
 *
 * **`p_bike` と `p_dock` はここに複製しない。** 同じ数を 2 か所に置くと、どちらが正かが
 * 決まらない（契約 4 と同じ理由）。`trip` が非 null なら両端の `forecast` が在ることが
 * 保証されるので、呼ぶ側は `from.forecast.p_bike` と `to.forecast.p_dock` を読む。
 */
export const tripOutcomeSchema = z.object({
  /** 出発で借りられ、到着で返せる確率（0〜1）。**独立仮定**（`notice` を参照）。 */
  p_trip: z.number().min(0).max(1),
  /**
   * 0〜3。**両端の小さいほう。** 鎖は弱い環の強さしかない。
   */
  confidence: z.number().int().min(0).max(3),
  /**
   * **独立仮定の注記。** 数と同じ欄に置く——別の場所に置くと、数だけ取り出して
   * 注記を落とせてしまう。文言は `TRIP_INDEPENDENCE_NOTICE`。
   */
  notice: z.string().min(1),
});

/**
 * 代替のポート候補（W4-25）。
 *
 * **同一システム・400 m 以内**で、**その端点と同じ時刻の確率が高い順**に最大 5 件。
 * 予測を出せないポートは入れない（確率を答えられない候補は問いに答えていない）。
 *
 * **「端点より良い」では絞っていない。** 出すかどうかは画面の判断で、API は材料を渡す。
 */
export const tripAlternativeSchema = z.object({
  /** 端点からの距離（m）。`station_neighbors` が日次で計算した値。 */
  distance_m: z.number().int().nonnegative(),
  /** **`forecast` は、その端点と同じ時刻**（出発側なら `depart_in_min`）で出してある。 */
  station: stationCurrentSchema,
});

/**
 * `/v1/trip-check` の応答（W4 プラン §6.7）。
 *
 * **確率が指す時刻は 2 つある。**
 *   * `from.forecast` … `generated_at ＋ depart_in_min`（借りる時刻）
 *   * `to.forecast` … `generated_at ＋ arrive_in_min`（返す時刻）
 *
 * どちらも `generated_at` からの相対で、**読んだ時刻からではない**。応答が CDN に
 * 留まっていた時間だけ離れる（最大 3 分）ので、**表示は絶対時刻で行う**
 * （W4 プラン §12 の 114）。
 */
export const tripCheckResponseSchema = z.object({
  api_version: z.literal("v1"),
  generated_at: z.iso.datetime(),
  /** いずれかのフィードの観測が途切れている。 */
  stale: z.boolean(),
  /** **行程は 1 つの系統の中で完結する。** 事業者をまたぐ行程は成立しない（W4-21）。 */
  system_id: systemIdSchema,
  /** 出発ポートに着く時刻（**5 分に丸めた後**）。 */
  depart_in_min: z.number().int().positive(),
  /** 実際に使った乗車時間（分）。 */
  ride_min: z.number().int().nonnegative(),
  /** **サーバーが概算したか。** true なら直線距離 ÷ 14 km/h で、実際の経路より短い。 */
  ride_min_estimated: z.boolean(),
  /** 到着ポートに着く時刻。`depart_in_min ＋ ride_min`。 */
  arrive_in_min: z.number().int().nonnegative(),
  from: stationCurrentSchema,
  to: stationCurrentSchema,
  /** **片側でも予測が欠けたら null**（契約 10）。 */
  trip: tripOutcomeSchema.nullable(),
  alternatives: z.object({
    from: z.array(tripAlternativeSchema),
    to: z.array(tripAlternativeSchema),
  }),
  feeds: z.array(feedStatusSchema),
  attribution: z.array(attributionSchema),
});

/**
 * ポート詳細の予測曲線（`/v1/stations/{system}/{station_id}`。W5-13）。
 *
 * **補間しない。** `station_forecasts` の**生の 10 点**をそのまま返す。地図
 * （`/v1/stations`）が返すのは「その到着時刻の 1 点」で、**正はどちらも
 * `station_forecasts`**——同じ確率を 2 つの形で配らないための切り分けである（契約 4・16）。
 *
 * **`horizons_min` と `p_bike` / `p_dock` は同じ長さ**で、添字が対応する。
 * `generated_at` からの分数なので、**読んだ時刻からではない**（W4 プラン §12 の 114）。
 */
export const forecastCurveSchema = z.object({
  /** **鮮度はこれで測る。** どの観測に基づくか。 */
  base_observed_at: z.iso.datetime(),
  /** **水平の起点。** `horizons_min[i]` はここからの分数。 */
  generated_at: z.iso.datetime(),
  model_version: z.string().min(1),
  /** 0〜3。3 が最も確か（履歴が過半の水平で効いた）。 */
  confidence: z.number().int().min(0).max(3),
  horizons_min: z.array(z.number().int().positive()).min(1),
  /** 借りられる確率（0〜1）。`horizons_min` と同じ長さ。 */
  p_bike: z.array(z.number().min(0).max(1)).min(1),
  /** 返せる確率（0〜1）。 */
  p_dock: z.array(z.number().min(0).max(1)).min(1),
});

/**
 * 直近 24 時間の実績（1 時間ごと。migration 0046）。
 *
 * **観測の無かった時間帯は行ごと出さない。** 0 を入れると「0 台だった」に見える
 * （CLAUDE.md §6 の「補間しない」と同じ規律）。だから**最大 24 件で、それより少ない**。
 *
 * `n` は**貸出と返却の両方が観測できた**回数で、2 つの平均は同じ母数の上にある。
 */
export const recentHourSchema = z.object({
  /** その時間の始まり（UTC）。 */
  hour_start: z.iso.datetime(),
  /** 両方が観測できたスナップショットの数。 */
  n: z.number().int().positive(),
  bikes_mean: z.number().nonnegative(),
  docks_mean: z.number().nonnegative(),
});

/**
 * `/v1/stations/{system}/{station_id}` の応答（開発プラン §8.3 の 2 行目、W5 の PR F）。
 *
 * **`at` を受けない。** 曲線を返すので到着時刻が要らず、**受けると同じポートの URL が
 * 5 分ごとに割れて CDN が効かない**。
 *
 * **欄ごとに optional**（契約 2）。予測がまだ無いポートは `forecast_curve` が null に
 * なるだけで、200 を返す。
 */
export const stationDetailResponseSchema = z.object({
  api_version: z.literal("v1"),
  generated_at: z.iso.datetime(),
  /** そのポートのフィードの観測が途切れている。 */
  stale: z.boolean(),
  station: stationCurrentSchema,
  /** **生の 10 点**（補間しない。W5-13）。予測が無い・古ければ null。 */
  forecast_curve: forecastCurveSchema.nullable(),
  /** 直近 24 時間の実績（**最大 24 件**。観測の無い時間帯は出さない）。 */
  recent: z.array(recentHourSchema).max(24),
  /** **そのポートのシステムだけ**。地図と違い 1 系統しか関係しない。 */
  feeds: z.array(feedStatusSchema),
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
export type StationForecast = z.infer<typeof stationForecastSchema>;
export type StationCurrent = z.infer<typeof stationCurrentSchema>;
export type StationsResponse = z.infer<typeof stationsResponseSchema>;
export type TripOutcome = z.infer<typeof tripOutcomeSchema>;
export type TripAlternative = z.infer<typeof tripAlternativeSchema>;
export type TripCheckResponse = z.infer<typeof tripCheckResponseSchema>;
export type ForecastCurve = z.infer<typeof forecastCurveSchema>;
export type RecentHour = z.infer<typeof recentHourSchema>;
export type StationDetailResponse = z.infer<typeof stationDetailResponseSchema>;
export type Problem = z.infer<typeof problemSchema>;
