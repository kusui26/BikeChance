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
  /**
   * `capacity` が固定のラック数ではなく `bikes + docks` の動的値であること（開発プラン §3.6）。
   *
   * **正は DB の `systems.capacity_is_dynamic`**（migration 0014）で、`/v1` は
   * `v1_feeds` から読む。ここは DB に届かないときの控えとして持つ。
   */
  readonly capacity_is_dynamic: boolean;
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
    // 非標準の vehicle_capacity（文字列）を数値化した固定の定員
    capacity_is_dynamic: false,
  },
  "docomo-cycle": {
    system_id: "docomo-cycle",
    display_name: "ドコモ・バイクシェア",
    operator_name: "株式会社ドコモ・バイクシェア / 公共交通オープンデータ協議会",
    operator_name_ascii: "DOCOMO BIKESHARE, INC.",
    dataset_name: "ドコモ・バイクシェア バイクシェア関連情報",
    license_url: "https://creativecommons.org/licenses/by/4.0/deed.ja",
    // 24 時間の実測で 1,068 回公開（86,400 ÷ 1,068 ＝ 80.9 秒）。中央値は 80 秒だが、
    // 1 周期とばす回が 16 回あり平均は 81 秒になる（W1 プラン §5.6 の 43）
    expected_cadence_s: 81,
    poll_interval_s: 60,
    // capacity は bikes + docks で毎回動く。SCD2 の比較から外してある（W1 プラン §5.6 の 45）
    capacity_is_dynamic: true,
  },
};

/** 日本の外接矩形。範囲外の座標は geo_suspect として扱う（§3.6）。 */
export const JAPAN_BBOX = {
  lat_min: 20,
  lat_max: 46,
  lon_min: 122,
  lon_max: 154,
} as const;

/**
 * 収集の設定。W1 プラン §6.2・§11.5 に対応する。
 * 単位は変数名に含める（CLAUDE.md 3）。
 */

/** GBFS のフィード名。W1 で収集するのは status（毎分）と information（日次）の 2 つ。 */
export const FEED_NAMES = ["station_status", "station_information"] as const;
export type FeedName = (typeof FEED_NAMES)[number];

/** ODPT の認証付きエンドポイント。キーはクエリ acl:consumerKey でしか受け付けない（開発プラン D-04）。 */
export const ODPT_TOKEN_BASE_URL = "https://api.odpt.org/api/v4/gbfs";

/** ODPT の公開エンドポイント。認証付きが失敗したときのフォールバック。応答は認証付きと同一。 */
export const ODPT_PUBLIC_BASE_URL = "https://api-public.odpt.org/api/v4/gbfs";

/** ODPT への 1 回の取得のタイムアウト。認証付き → 公開の 2 回で最悪 40 秒。 */
export const ODPT_FETCH_TIMEOUT_MS = 20_000;

/** 生データを置く Storage バケット。非公開で、サービスロールからのみ読み書きする。 */
export const RAW_BUCKET = "gbfs-raw";

/** 生データの gzip に付ける Content-Type。省略すると text/plain になる（W1 プラン §4.3 の 9）。 */
export const GZIP_CONTENT_TYPE = "application/gzip";

/** ODPT から見て誰の取得かが分かるようにする。連絡先は環境変数で差し替える（§5.1 の 18）。 */
export const USER_AGENT_PRODUCT = "BikeChance/0.1";

/** 収集ジョブの最大実行時間（秒）。Vercel の maxDuration に渡す。 */
export const COLLECT_MAX_DURATION_S = 60;

/**
 * 属性同期ジョブの最大実行時間（秒）。収集より長くとる。
 * station_information は HELLO で 7.8 MB あり、取得・展開・14,900 件の比較を 1 回で行うため。
 */
export const SYNC_STATIONS_MAX_DURATION_S = 120;

/** 属性同期の Cron（UTC）。04:00 JST。収集の毎分と重ならない時刻に置く。 */
export const SYNC_STATIONS_CRON = "0 19 * * *";

/**
 * 天気予報アーカイブの設定（W2 プラン §5.3、§9.1）。
 *
 * 学習で使うのは「その時刻に入手できた**予報**」で、過去に遡って観測できない。
 * ここは**保存だけ**を担い、解釈（テーブル化・特徴量）は W4 で行う。
 */

/** Open-Meteo のライブ予報。アーカイブの本体はここから取る。 */
export const OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast";

/**
 * 取得するモデル。**2 つ要求する。**
 *
 * `jma_msm` は気象庁 MSM（約 5 km・1 時間値。開発プラン §6.4）で、日本の陸上では
 * これが最も細かい。ただし**降水確率を返さない**（実測で `precipitation_probability`
 * が全件 null）。§6.3 の `precip_prob_fcst` はこれだけでは作れない。
 *
 * `best_match` は Open-Meteo が地域ごとに選ぶ混合で、**`jma_msm` の別名ではない**。
 * `precipitation_probability` はこちらにしか入らない。
 *
 * **決定論的な値の一致は緯度で決まる**（W3 プラン §12 の 78。371,280 値の実測）。
 *
 * | 緯度帯 | 降水量の食い違い |
 * |---|---|
 * | 20〜30 度（沖縄・奄美） | 44〜48% |
 * | **30〜40 度（本州・四国・九州）** | **0.00%**（259,272 値） |
 * | 40〜45 度（北海道北部） | 21% |
 *
 * つまり**ポートのほとんどが乗る本州域では両者は完全に一致し、端で分かれる**。
 * 該当するポートは全 20,742 の 2% 前後。**どちらを特徴量に使うかは W4 で決める**ので、
 * それまで両方を保存し続ける（後から遡って足せない）。
 */
export const OPEN_METEO_MODELS = ["jma_msm", "best_match"] as const;

/** アーカイブのパスに使う名前。モデルを増やしても保存先の規約は変えない。 */
export const OPEN_METEO_MODEL = "jma_msm";

/**
 * 取得する変数。**後から遡って足せない**ので、§6.3 の天気特徴量に要るものを最初から全部取る。
 * 複数モデルを要求すると、応答の系列名にモデル名の接尾辞が付く
 * （`precipitation_jma_msm` / `precipitation_best_match`）。
 */
export const OPEN_METEO_HOURLY_VARIABLES = [
  "precipitation",
  "precipitation_probability",
  "temperature_2m",
  "wind_speed_10m",
  "weather_code",
] as const;

/**
 * 予報の期間（日）。**「取得時刻から先の日数」ではなく「今日から数えた日数」**である。
 *
 * 窓は取得時刻によらず **JST の当日 0 時から始まる**ので、**先の覆いは時刻とともに縮む**
 * （データ辞書 §8.2）。`2` のときは 00:17 で 48 時間・23:17 で約 25 時間しか無かった。
 *
 * **`3` にする理由**（W3 プラン §12 の 77。100 格子で実測）。
 *
 * | 日数 | 時間数 | gzip | 所要 | `jma_msm` の欠損 |
 * |---|---|---|---|---|
 * | 2 | 48 | 30,001 B | 2.30 秒 | 0 |
 * | **3** | **72** | **43,423 B** | **2.44 秒** | **0** |
 * | 4 | 96 | 55,355 B | 2.50 秒 | 2,000（20.8%） |
 * | 7 | 168 | 78,789 B | 2.82 秒 | 9,200（54.8%） |
 *
 * **`jma_msm` が値を返すのは当日 0 時から 76 時間ちょうど**で、そこから先は全格子
 * いっせいに null になる（`forecast_days=4` で最初の欠損 index が全格子とも 76）。
 * **3 が「`jma_msm` が欠けない最大」**である。4 以上にすると `best_match` しか値の
 * 無い区間ができ、系列の途中でモデルが替わる。
 *
 * 費用は容量 +45%（4.4 → 約 6.4 MB/日、年 2.3 GB）、所要 +6%、呼び出し回数は不変。
 * **天気は唯一あとから遡って取れない入力**なので、安いうちに広げる。
 *
 * **`time` の長さを定数として扱わない。** この値を変えた前後でファイルの長さが変わる
 * （2026-09-09 より前は 48 点、以降は 72 点）。読む側は必ず配列の長さを見る。
 */
export const OPEN_METEO_FORECAST_DAYS = 3;

/**
 * `jma_msm` が値を返す最大の時間数（当日 0 時から数えて）。実測で 76 時間ちょうど。
 * `OPEN_METEO_FORECAST_DAYS` をこれ以上に広げると欠損が混じる。
 */
export const JMA_MSM_COVERAGE_HOURS = 76;

/**
 * jma_msm の格子の刻み。要求した座標はこの格子に丸められて返る
 * （実測：35.681 → 35.7、139.767 → 139.75）。**SQL 側にも同じ値を渡す**
 * （`weather_grid_cells` の引数）ので、値の出どころはここ 1 箇所。
 */
export const WEATHER_GRID_LAT_STEP = 0.05;
export const WEATHER_GRID_LON_STEP = 0.0625;

/**
 * 1 回の要求にまとめる格子の数。Open-Meteo は複数地点を配列で返す。
 * 実測で 100 地点は HTTP 200・214 KB・2.0 秒。595 格子なら 6 要求で済む。
 */
export const WEATHER_BATCH_SIZE = 100;

/** 1 要求のタイムアウト。6 要求を順に投げても maxDuration に収まる値。 */
export const WEATHER_FETCH_TIMEOUT_MS = 15_000;

/** 生の予報を置く Storage バケット。非公開で、サービスロールからのみ読み書きする。 */
export const WEATHER_BUCKET = "weather-raw";

/** 天気アーカイブジョブの最大実行時間（秒）。Vercel の maxDuration に渡す。 */
export const ARCHIVE_WEATHER_MAX_DURATION_S = 120;

/**
 * 天気アーカイブの Cron（UTC）。毎時 17 分。
 * Parquet 化（毎時 7 分）と時刻の先頭を避け、重い処理が同時に走らないようにする。
 */
export const ARCHIVE_WEATHER_CRON = "17 * * * *";

/**
 * 毎時 Parquet 化の設定（W2 プラン §5.6、§9.2）。
 *
 * 実体は Python（`apps/ml`）にあり、この定数を import できない。値は
 * `apps/ml/bikechance_ml/io/supabase.py` と `supabase/migrations/..._0017_parquet_bucket.sql`
 * にも書かれている。**食い違いは `vercel-crons.test.ts` と本番の 404 で気づく**ので、
 * ここは「TypeScript 側から見た正」を置く場所として使う。
 */

/** 学習用 Parquet を置く Storage バケット。非公開で、サービスロールからのみ読み書きする。 */
export const PARQUET_BUCKET = "gbfs-parquet";

/**
 * Parquet 化の Cron（UTC）。毎時 7 分。
 *
 * 前 1 時間を畳むので、その時間帯の観測がすべて入り終わってから動かす必要がある。
 * ドコモの停滞閾値（W1-42）が最大 4 分強なので、7 分あれば取りこぼさない。
 * 天気アーカイブ（17 分）と時刻をずらし、重い処理を同時に走らせない。
 */
export const COMPACT_CRON = "7 * * * *";

/**
 * Parquet 化の最大実行時間（秒）。`vercel.json` の services.ml.functions に渡す。
 * 見込みは 1 回 10 秒未満（44 万行）。ネットワークの揺れを見込んで 120 秒とる。
 */
export const COMPACT_MAX_DURATION_S = 120;

/**
 * 先回り推論の Cron（UTC。開発プラン §8.1、W3 プラン §5.10）。
 *
 * **フィードの公開周期に位相を合わせる。** HELLO の `last_updated` は毎時 :01:34 から
 * 5 分周期で、公開は約 55 秒後、収集器は毎分起動なので **:03:59 までに DB に入る**。
 * 推論を `4-59/5`（:04, :09, …）にすると、常に最新スナップショットの 1〜2 分後に
 * 予測できる。ドコモ（80 秒周期）は 5 分グリッド毎に 1 回でよいので `1-59/5`。
 *
 * **段階的に入れた（W3-19）。** HELLO だけを 11 時間 5 分・134 サイクル動かし、
 * 失敗 0・HOT 率 100.0%・`n_dead_tup` が 1 周期ぶんで頭打ちになることを確かめてから
 * ドコモを足した（W3 プラン §5.10）。**いまは両系が `vercel.json` に載っている。**
 */
export const INFER_CRON: Readonly<Record<SystemId, string>> = {
  hellocycling: "4-59/5 * * * *",
  "docomo-cycle": "1-59/5 * * * *",
};

/**
 * 日次の参照スナップショット（UTC。W3 プラン §14.3）。
 *
 * **`rebuild_geo`（pg_cron `30 19 * * *` ＝ 04:30 JST）の後に走らせる。** 近傍と
 * 行政区画がその日ぶん更新された状態を固めたいので、順番が意味を持つ。属性同期
 * （04:00 JST）→ `rebuild_geo`（04:30 JST）→ ここ（**05:00 JST**）。
 *
 * 前日ぶんを書くので、当日 05:00 の時点で対象の 24 時間はすべて畳み終わっている
 * （毎時 :07 の Parquet 化）。
 */
export const REFERENCE_CRON = "0 20 * * *";

/**
 * 推論の最大実行時間（秒）。20,745 ポート × 10 水平 × 2 指標を作って書き戻す。
 * ベースライン（B3）は行列演算だけなので見込みは 10 秒未満だが、Storage からの
 * 成果物の取得と 6 往復の UPSERT を含めて 120 秒とる。
 */
export const INFER_MAX_DURATION_S = 120;

/**
 * 公開 API `/v1` の設定（開発プラン §8.3、W2 プラン §5.7）。
 */

/** `/v1` の応答の CDN キャッシュ。1 分保持し、その後 2 分は古い値を返しつつ裏で更新する。 */
export const V1_CACHE_CONTROL = "public, s-maxage=60, stale-while-revalidate=120";

/**
 * `/v1` のハンドラの最大実行時間（秒）。
 * DB が詰まったときに 300 秒（Vercel の既定）待たされないよう、短く切る。
 */
export const V1_MAX_DURATION_S = 10;

/** `/v1` から DB へ 1 回問い合わせるときのタイムアウト。maxDuration の内側に収める。 */
export const V1_QUERY_TIMEOUT_MS = 5_000;
