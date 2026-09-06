/**
 * GBFS の station_status を、取り込みに使う形へ正規化する（W1 プラン §6.4、§11.1）。
 *
 * ここは**純粋関数だけ**。I/O を持たないのでフィクスチャで完全にテストできる。
 *
 * 出力するのは「今回のフィードに現れたポート」の分だけで、`-1` を埋めた密な配列は作らない。
 * 密化は `ingest_snapshot`（PR B）の仕事で、そちらは `stations.idx` を知っている。
 * TypeScript 側は idx を知らなくてよい、というのがこの分担の狙い。
 */
import { SMALLINT_MAX, type StationStatusEntry, type StationStatusFeed } from "./schemas";

/** フラグのビット位置（§11.1）。すべて真なら 7。 */
export const FLAG_INSTALLED = 1;
export const FLAG_RENTING = 2;
export const FLAG_RETURNING = 4;

/** 観測時刻とポートの報告時刻の差の上限。smallint に収める。 */
export const MAX_REPORTED_AGE_S = SMALLINT_MAX;

export type NormalizedStation = {
  readonly station_id: string;
  readonly bikes: number;
  readonly docks: number;
  readonly flags: number;
  readonly reported_age_s: number;
};

/**
 * 正規化の過程で気づいたこと。`feed_fetch_log.warnings` に残す（§5 の 16）。
 * 0 のままなら何も無かったということなので、そのまま記録してよい。
 */
export type NormalizeWarnings = {
  /** 同じ station_id で、取り込む値も同じだったもの。先頭を残して捨てた件数 */
  readonly exact_duplicates: number;
  /** 同じ station_id で、取り込む値が違ったもの。先頭を残したため情報を捨てている */
  readonly conflicting_duplicates: number;
  /** last_reported が無く、observed_at と同時刻とみなした件数 */
  readonly missing_last_reported: number;
  /** reported_age_s を 0 または上限に丸めた件数（ODPT 側の時計ずれ） */
  readonly clamped_reported_age: number;
  /**
   * 負の台数・返却枠を 0 に丸めた件数。HELLO の定員超過ポートで実際に起きる
   * （実測 17 件）。0 でない日が続くなら、容量の解釈を見直す手がかりになる
   */
  readonly clamped_negative_counts: number;
};

export type NormalizedFeed = {
  /** フィードの last_updated（POSIX 秒）。 */
  readonly observed_at_s: number;
  readonly stations: readonly NormalizedStation[];
  readonly warnings: NormalizeWarnings;
};

export const toFlags = (entry: StationStatusEntry): number =>
  (entry.is_installed ? FLAG_INSTALLED : 0) +
  (entry.is_renting ? FLAG_RENTING : 0) +
  (entry.is_returning ? FLAG_RETURNING : 0);

/**
 * `observed_at − last_reported` の秒数。
 * **負値は 0 に丸める**。`-1` は「登録済みだが今回現れなかった」専用の値で、
 * ODPT 側の時計ずれと衝突させてはいけない（§5 の 5）。
 */
export const toReportedAgeSeconds = (observed_at_s: number, last_reported_s: number): number => {
  const age = observed_at_s - last_reported_s;
  if (age < 0) return 0;
  return age > MAX_REPORTED_AGE_S ? MAX_REPORTED_AGE_S : age;
};

/**
 * 負の台数・返却枠を 0 に丸める。
 *
 * HELLO は定員超過のポートで `num_docks_available` に -1 を返す（実測 17 件）。
 * **`-1` は「登録済みだが今回のフィードに現れなかった」を表す予約値**なので、
 * そのまま保存すると「返却枠が -1」と「そもそも観測していない」が区別できなくなる（§11.1）。
 *
 * 0 に丸めるのは意味の上でも正しい。定員を超えて停まっているポートに返す枠は無い。
 * 「何台超過しているか」は W3 の特徴量で `capacity − bikes` から導ける。
 */
export const clampCount = (value: number): number => (value < 0 ? 0 : value);

const toNormalized = (entry: StationStatusEntry, observed_at_s: number): NormalizedStation => ({
  station_id: entry.station_id,
  bikes: clampCount(entry.num_bikes_available),
  docks: clampCount(entry.num_docks_available),
  flags: toFlags(entry),
  reported_age_s:
    entry.last_reported === undefined
      ? 0
      : toReportedAgeSeconds(observed_at_s, entry.last_reported),
});

const isSameValues = (a: NormalizedStation, b: NormalizedStation): boolean =>
  a.bikes === b.bikes &&
  a.docks === b.docks &&
  a.flags === b.flags &&
  a.reported_age_s === b.reported_age_s;

const wasClamped = (entry: StationStatusEntry, observed_at_s: number): boolean =>
  entry.last_reported !== undefined &&
  (observed_at_s - entry.last_reported < 0 ||
    observed_at_s - entry.last_reported > MAX_REPORTED_AGE_S);

/**
 * 重複した station_id は**先頭を残す**（§5 の 16）。
 * ドコモには完全重複が実在する（2026-09-06 の実測で 11 件）。
 * 取り込む値が違う重複は情報を捨てているので、件数を警告に残す。
 */
export const normalizeStationStatus = (feed: StationStatusFeed): NormalizedFeed => {
  const observed_at_s = feed.last_updated;
  const byId = new Map<string, NormalizedStation>();
  let exact_duplicates = 0;
  let conflicting_duplicates = 0;
  let missing_last_reported = 0;
  let clamped_reported_age = 0;
  let clamped_negative_counts = 0;

  for (const entry of feed.data.stations) {
    if (entry.last_reported === undefined) missing_last_reported += 1;
    if (wasClamped(entry, observed_at_s)) clamped_reported_age += 1;
    if (entry.num_bikes_available < 0 || entry.num_docks_available < 0) {
      clamped_negative_counts += 1;
    }

    const normalized = toNormalized(entry, observed_at_s);
    const existing = byId.get(normalized.station_id);
    if (existing === undefined) {
      byId.set(normalized.station_id, normalized);
    } else if (isSameValues(existing, normalized)) {
      exact_duplicates += 1;
    } else {
      conflicting_duplicates += 1;
    }
  }

  return {
    observed_at_s,
    // Map は挿入順を保つため、フィードに現れた順がそのまま残る
    stations: [...byId.values()],
    warnings: {
      exact_duplicates,
      conflicting_duplicates,
      missing_last_reported,
      clamped_reported_age,
      clamped_negative_counts,
    },
  };
};

/** 警告が 1 つでもあるか。ログに残すかどうかの判断に使う。 */
export const hasWarnings = (warnings: NormalizeWarnings): boolean =>
  Object.values(warnings).some((count) => count > 0);
