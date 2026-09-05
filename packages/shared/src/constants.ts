/** プロジェクト全体で共有する定数。マジックナンバーはここに集約する（CLAUDE.md 3）。 */

/** 予測の水平（分）。短期モデルはこの 10 点を出力し、間は線形補間する。 */
export const HORIZONS_MIN = [5, 10, 15, 20, 30, 45, 60, 90, 120, 180] as const;

/** 学習・推論の時間グリッド（分）。 */
export const GRID_INTERVAL_MIN = 5;

/** 長期プロファイルモデルが出力する時間数（7 日 × 24 時間）。 */
export const PROFILE_HOURS = 168;

/** 確率の表示帯。境界値は §9.3 の表に対応する。 */
export const PROBABILITY_BAND_THRESHOLDS = {
  high_min: 0.85,
  medium_min: 0.6,
} as const;

/** 表示する確率の上限。100% とは言い切らない。 */
export const PROBABILITY_DISPLAY_MAX = 0.99;

/** 確率の表示刻み（過度な精度を見せない）。 */
export const PROBABILITY_DISPLAY_STEP = 0.05;

/** 予測が古いと判断するまでの秒数。超えたら API は stale を返す。 */
export const FORECAST_STALE_AFTER_S = 15 * 60;

export const SYSTEM_IDS = ["hellocycling", "docomo-cycle"] as const;
export type SystemId = (typeof SYSTEM_IDS)[number];

export type SystemDefinition = {
  readonly system_id: SystemId;
  readonly display_name: string;
  /** クレジット表記に使う提供者名（CKAN のデータセットページの記載に従う）。 */
  readonly operator_name: string;
  /** HTTP ヘッダ用の ASCII 表記。ヘッダ値は Latin-1 しか運べない。 */
  readonly operator_name_ascii: string;
  /** クレジット表記に使うデータセット名。 */
  readonly dataset_name: string;
  readonly license_url: string;
  /** フィードの実測更新周期（秒）。監視の期待値に使う。 */
  readonly expected_cadence_s: number;
  /** 収集のポーリング間隔（秒）。 */
  readonly poll_interval_s: number;
};

export const SYSTEMS: Readonly<Record<SystemId, SystemDefinition>> = {
  hellocycling: {
    system_id: "hellocycling",
    display_name: "HELLO CYCLING",
    operator_name: "OpenStreet株式会社 / 公共交通オープンデータ協議会",
    operator_name_ascii: "OpenStreet Corp.",
    dataset_name: "OpenStreet（ハローサイクリング） バイクシェア関連情報",
    license_url: "https://creativecommons.org/licenses/by/4.0/deed.ja",
    expected_cadence_s: 300,
    poll_interval_s: 60,
  },
  "docomo-cycle": {
    system_id: "docomo-cycle",
    display_name: "ドコモ・バイクシェア",
    operator_name: "株式会社ドコモ・バイクシェア / 公共交通オープンデータ協議会",
    operator_name_ascii: "DOCOMO BIKESHARE, INC.",
    dataset_name: "ドコモ・バイクシェア バイクシェア関連情報",
    license_url: "https://creativecommons.org/licenses/by/4.0/deed.ja",
    expected_cadence_s: 80,
    poll_interval_s: 60,
  },
};

/** 日本の外接矩形。範囲外の座標は geo_suspect として扱う（§3.6）。 */
export const JAPAN_BBOX = {
  lat_min: 20,
  lat_max: 46,
  lon_min: 122,
  lon_max: 154,
} as const;
