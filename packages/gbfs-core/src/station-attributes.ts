/**
 * GBFS の station_information を、属性同期に使う形へ正規化する（W1 プラン §6.9、§11.1）。
 *
 * `normalize.ts` と同じく**純粋関数だけ**。I/O を持たない。
 *
 * status と違って密な配列は作らない。属性は SCD2 の履歴（`station_attributes`）に入り、
 * 「今回のフィードに現れたポート」の分だけを送る。フィードに現れなかったポートの有効行は
 * **閉じない**（一時的な欠落で属性を失わないため。判断は W2 以降）。
 *
 * 容量の扱いが両システムで違う（開発プラン §3.6）。
 *   ドコモ  `capacity` は数値だが**固定ラック数ではなく `bikes + docks` の動的値**
 *   HELLO   `capacity` が無く、非標準の `vehicle_capacity`（**数字の文字列**）だけ
 * どちらも `capacity` 列にそのまま入れ、意味の解釈は特徴量側で行う。
 */
import { JAPAN_BBOX } from "@bikechance/shared";
import { SMALLINT_MAX, type StationInformationEntry, type StationInformationFeed } from "./schemas";

export type NormalizedAttributes = {
  readonly station_id: string;
  readonly name: string;
  readonly lat: number;
  readonly lon: number;
  /** smallint に収めた容量。読み取れなければ null。 */
  readonly capacity: number | null;
  /**
   * 日本の外接矩形の外にある座標（開発プラン §14）。地図には出さず、時系列は保持する。
   * ドコモに実在する（`4826` は経度が 139.553764 ではなく 39.553764 で、先頭の 1 が落ちている）。
   */
  readonly geo_suspect: boolean;
  /** GBFS のオブジェクト全体。未知フィールドを保全する（`raw` 列に入る）。 */
  readonly raw: StationInformationEntry;
};

/** 正規化の過程で気づいたこと。`job_runs.detail` に残す。 */
export type AttributeWarnings = {
  /** 同じ station_id で、比較対象の値も同じだったもの。先頭を残して捨てた件数 */
  readonly exact_duplicates: number;
  /** 同じ station_id で、比較対象の値が違ったもの。先頭を残したため情報を捨てている */
  readonly conflicting_duplicates: number;
  /** capacity も vehicle_capacity も無かった件数（HELLO のバーチャルポート等） */
  readonly missing_capacity: number;
  /** vehicle_capacity が数値に変換できなかった件数。仕様変更の早期検知に使う */
  readonly unparsable_capacity: number;
  /** capacity を smallint の範囲に丸めた件数 */
  readonly clamped_capacity: number;
  /**
   * 日本の外接矩形の外にある座標の件数（開発プラン §14 の geo_suspect）。
   * ドコモで恒常的に 1 件。0 に戻ったら提供側が直したということ
   */
  readonly outside_japan: number;
};

export type NormalizedInformation = {
  /** フィードの last_updated（POSIX 秒）。 */
  readonly observed_at_s: number;
  readonly stations: readonly NormalizedAttributes[];
  readonly warnings: AttributeWarnings;
};

/** 容量の読み取り結果。読めなかった理由を呼び出し側が数えられるようにする。 */
type CapacityOutcome = {
  readonly capacity: number | null;
  readonly missing: boolean;
  readonly unparsable: boolean;
  readonly clamped: boolean;
};

const NOT_PRESENT: CapacityOutcome = {
  capacity: null,
  missing: true,
  unparsable: false,
  clamped: false,
};

const clampCapacity = (value: number): CapacityOutcome => {
  const truncated = Math.trunc(value);
  const clamped = truncated < 0 || truncated > SMALLINT_MAX;
  const capacity = truncated < 0 ? 0 : truncated > SMALLINT_MAX ? SMALLINT_MAX : truncated;
  return { capacity, missing: false, unparsable: false, clamped };
};

/**
 * ドコモの `capacity`（数値）を優先し、無ければ HELLO の `vehicle_capacity`（文字列）を読む。
 * 両方無いのは異常ではない（バーチャルポートには容量が無い）。
 */
export const toCapacity = (entry: StationInformationEntry): CapacityOutcome => {
  if (entry.capacity !== undefined) {
    return clampCapacity(entry.capacity);
  }
  if (entry.vehicle_capacity === undefined) {
    return NOT_PRESENT;
  }
  const parsed =
    typeof entry.vehicle_capacity === "number"
      ? entry.vehicle_capacity
      : Number(entry.vehicle_capacity);
  if (!Number.isFinite(parsed)) {
    return { capacity: null, missing: false, unparsable: true, clamped: false };
  }
  return clampCapacity(parsed);
};

/** 日本の外接矩形の中にあるか（§3.6）。外なら座標の取り違えを疑う。 */
export const isInsideJapan = (lat: number, lon: number): boolean =>
  lat >= JAPAN_BBOX.lat_min &&
  lat <= JAPAN_BBOX.lat_max &&
  lon >= JAPAN_BBOX.lon_min &&
  lon <= JAPAN_BBOX.lon_max;

const isSameValues = (a: NormalizedAttributes, b: NormalizedAttributes): boolean =>
  a.name === b.name && a.lat === b.lat && a.lon === b.lon && a.capacity === b.capacity;

/**
 * 重複した station_id は**先頭を残す**（`normalizeStationStatus` と同じ規則）。
 * 比較する 4 つの値が同じなら exact、違えば conflicting として数える。
 */
export const normalizeStationInformation = (
  feed: StationInformationFeed,
): NormalizedInformation => {
  const byId = new Map<string, NormalizedAttributes>();
  let exact_duplicates = 0;
  let conflicting_duplicates = 0;
  let missing_capacity = 0;
  let unparsable_capacity = 0;
  let clamped_capacity = 0;
  let outside_japan = 0;

  for (const entry of feed.data.stations) {
    const outcome = toCapacity(entry);
    if (outcome.missing) missing_capacity += 1;
    if (outcome.unparsable) unparsable_capacity += 1;
    if (outcome.clamped) clamped_capacity += 1;
    const geo_suspect = !isInsideJapan(entry.lat, entry.lon);
    if (geo_suspect) outside_japan += 1;

    const normalized: NormalizedAttributes = {
      station_id: entry.station_id,
      name: entry.name,
      lat: entry.lat,
      lon: entry.lon,
      capacity: outcome.capacity,
      geo_suspect,
      raw: entry,
    };
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
    observed_at_s: feed.last_updated,
    stations: [...byId.values()],
    warnings: {
      exact_duplicates,
      conflicting_duplicates,
      missing_capacity,
      unparsable_capacity,
      clamped_capacity,
      outside_japan,
    },
  };
};

/** 警告が 1 つでもあるか。 */
export const hasAttributeWarnings = (warnings: AttributeWarnings): boolean =>
  Object.values(warnings).some((count) => count > 0);
