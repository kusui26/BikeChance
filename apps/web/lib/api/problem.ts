/**
 * エラー応答（RFC 9457 Problem Details、開発プラン §8.3）。
 *
 * `/api/jobs/*` の `{ ok:false, title, detail }` とは形を変える。あちらは Cron が読む
 * 内部の応答で、こちらは iOS と Web が読む公開 API である。**公開側は標準に従う。**
 *
 * `type` は相対 URI（`/v1/problems/...`）にする。ドメインを決め打ちにせず、
 * `about:blank` で種類を潰すこともしない。機械が分岐するための `code` を拡張メンバに置く。
 */
import { problemSchema, type Problem } from "@bikechance/shared";

const PROBLEM_CONTENT_TYPE = "application/problem+json";

/** 応答の種類。増やすときは `/v1` のドキュメント（PR F のデータ辞書）にも足す。 */
export const PROBLEM_CODES = [
  "bbox_missing",
  "bbox_malformed",
  "bbox_out_of_range",
  "bbox_inverted",
  "bbox_too_large",
  "unknown_system",
  "too_many_stations",
  "arrival_conflict",
  "arrival_malformed",
  "arrival_out_of_range",
  // `/v1/trip-check`（W4 プラン §6.7）
  "station_missing",
  "unknown_station",
  "same_station",
  "depart_missing",
  "ride_malformed",
  "ride_out_of_range",
  "station_location_missing",
  "upstream_unavailable",
] as const;

export type ProblemCode = (typeof PROBLEM_CODES)[number];

const TITLES: Readonly<Record<ProblemCode, string>> = {
  bbox_missing: "bbox が指定されていません",
  bbox_malformed: "bbox の形式が正しくありません",
  bbox_out_of_range: "bbox が緯度経度の範囲外です",
  bbox_inverted: "bbox の南北または東西が逆です",
  bbox_too_large: "bbox が大きすぎます",
  unknown_system: "未知のシステムです",
  too_many_stations: "該当するポートが多すぎます",
  arrival_conflict: "at と in_min は同時に指定できません",
  arrival_malformed: "到着時刻の形式が正しくありません",
  arrival_out_of_range: "到着時刻が予測できる範囲の外です",
  station_missing: "ポートが指定されていません",
  unknown_station: "未知のポートです",
  same_station: "出発と到着が同じポートです",
  depart_missing: "出発時刻が指定されていません",
  ride_malformed: "乗車時間の形式が正しくありません",
  ride_out_of_range: "乗車時間が範囲外です",
  station_location_missing: "ポートの座標がありません",
  upstream_unavailable: "データベースに接続できません",
};

export const toProblem = (params: {
  readonly code: ProblemCode;
  readonly status: number;
  readonly detail: string;
}): Problem =>
  problemSchema.parse({
    type: `/v1/problems/${params.code.replaceAll("_", "-")}`,
    title: TITLES[params.code],
    status: params.status,
    detail: params.detail,
    code: params.code,
  });

/**
 * エラーは**キャッシュさせない**。bbox の誤りは呼び出し側が直せば消えるもので、
 * CDN に 60 秒残ると直したのに直らないように見える。
 */
export const problemResponse = (problem: Problem, headers: Readonly<Record<string, string>>) =>
  Response.json(problem, {
    status: problem.status,
    headers: { ...headers, "Content-Type": PROBLEM_CONTENT_TYPE, "Cache-Control": "no-store" },
  });
