/**
 * 正規化したポート列を `ingest_snapshot`（PR B、§11.3）の引数へ変換する。
 *
 * 4 本の配列は `p_station_ids` と**同じ長さ・同じ順序**でなければならない。
 * ここが崩れると、あるポートの台数が別のポートの行に入る。DB 側にも配列長の
 * CHECK 制約を置いてあるが（PR A）、順序の対応は型でも制約でも守れないので、
 * 「1 つのループで 5 本同時に積む」という書き方そのもので守る。
 */
import type { SystemId } from "@bikechance/shared";
import type { NormalizedFeed, NormalizedStation } from "./normalize";

/** `ingest_snapshot` に渡す配列群。名前は RPC の引数名に揃える。 */
export type IngestArrays = {
  readonly p_station_ids: readonly string[];
  readonly p_bikes: readonly number[];
  readonly p_docks: readonly number[];
  readonly p_flags: readonly number[];
  readonly p_reported_age_s: readonly number[];
};

/** `ingest_snapshot` の全引数。呼び出し側はこれをそのまま RPC に渡す。 */
export type IngestArgs = IngestArrays & {
  readonly p_system_id: SystemId;
  /** フィードの last_updated（ISO 8601）。timestamptz として渡す。 */
  readonly p_observed_at: string;
  readonly p_fetched_at: string;
  /** 応答の ETag。無ければ null。 */
  readonly p_etag: string | null;
  /** Storage 上の生 gzip JSON のパス（§11.5）。 */
  readonly p_raw_path: string;
};

const MS_PER_S = 1000;

/** POSIX 秒を timestamptz に渡せる ISO 8601（UTC）にする。 */
export const toIsoUtc = (epoch_s: number): string => new Date(epoch_s * MS_PER_S).toISOString();

/**
 * 5 本の配列を 1 つのループで同時に積む。
 * 別々に `map` すると、片方だけ filter を足したときに黙って対応が崩れる。
 */
export const buildIngestArrays = (stations: readonly NormalizedStation[]): IngestArrays => {
  const p_station_ids: string[] = [];
  const p_bikes: number[] = [];
  const p_docks: number[] = [];
  const p_flags: number[] = [];
  const p_reported_age_s: number[] = [];

  for (const station of stations) {
    p_station_ids.push(station.station_id);
    p_bikes.push(station.bikes);
    p_docks.push(station.docks);
    p_flags.push(station.flags);
    p_reported_age_s.push(station.reported_age_s);
  }

  return { p_station_ids, p_bikes, p_docks, p_flags, p_reported_age_s };
};

export const buildIngestArgs = (params: {
  readonly system_id: SystemId;
  readonly feed: NormalizedFeed;
  readonly fetched_at: Date;
  readonly etag: string | null;
  readonly raw_path: string;
}): IngestArgs => ({
  p_system_id: params.system_id,
  p_observed_at: toIsoUtc(params.feed.observed_at_s),
  p_fetched_at: params.fetched_at.toISOString(),
  p_etag: params.etag,
  p_raw_path: params.raw_path,
  ...buildIngestArrays(params.feed.stations),
});

/** 5 本の配列の長さが揃っているか。テストと、呼び出し側の最後の確認に使う。 */
export const hasConsistentLengths = (arrays: IngestArrays): boolean => {
  const expected = arrays.p_station_ids.length;
  return (
    arrays.p_bikes.length === expected &&
    arrays.p_docks.length === expected &&
    arrays.p_flags.length === expected &&
    arrays.p_reported_age_s.length === expected
  );
};
