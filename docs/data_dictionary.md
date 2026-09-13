# BikeChance データ辞書

- **目的**：**学習・推論のパイプラインを書く人が、この 1 枚だけを読んで入力仕様を決められるようにする。** 開発プラン（なぜ・何を作るか）と週次の実装プラン（どう作ったか）とは役割を分ける。ここに書くのは**データそのものの契約**である。
- **正の所在**：スキーマの正は `supabase/migrations`、パス規約の正は `packages/shared` の `storage-path.ts` / `weather-path.ts`、Parquet の正は `apps/ml/bikechance_ml/jobs/snapshot_table.py` の `SCHEMA`。**食い違ったらコードが正で、この文書を直す。**
- **数値**：断りのないものはすべて 2026-09-08 に本番で実測した値。推測は「見込み」と書く。
- **観測期間はまだ短い**：毎分収集は **2026-09-06 20:07 JST** から、天気は **2026-09-08 00:17 JST** から。**分布や「一度も現れていない」という記述は、この期間だけの話である。** 平日 2 日ぶんしか無く、休日・雨天・イベント日を含んでいない。データが貯まったら測り直し、日付を添えて書き換える。
- **更新規則**：列・パス・センチネルの意味を変えたら、**同じ PR でこの文書も直す**。実測値を足すときは測った日付を添える。
- **変更履歴**：**v1.23（2026-09-11）公開ビューを 1 枚足した（W4 の PR G、migration 0039）。§5 に **`v1_station_neighbors`**（500 m 以内のポートの組。`/v1/trip-check` の代替候補）。**絞り込みはビューに焼き付けない**——`distance_m <= 400` も `same_system` も「今日の `/v1/trip-check` の都合」であって近傍という事実の性質ではないので、絞るのは問い合わせ側。あわせて **`v1_stations_current` の `lat` / `lon` は NULL になりうる**ことを明記した：bbox で引けば範囲比較で外れるが、**ID で引くと出てくる**（本番に 10 件、うち 1 件は予測も持つ。W4 プラン §12 の 129）。** **v1.22（2026-09-10）モデルの成果物の書式を変えた（W4 の PR E′）。§4.14 の「モデルの成果物」を書き直し、**LightGBM は `format_version = 2`**——`Booster.model_to_string()`ではなく**木の構造そのもの**（節ごとの特徴量・閾値・左右・既定の向き・欠損の扱い・葉の値、カテゴリのビット集合）を持ち、配信側は `lightgbm` を読み込まずに numpy で歩く。**理由は大きさではなく、Vercel の Python ランタイムで `import lightgbm` が `OSError` になるから**（`libgomp.so.1` が無い。W4 プラン §12 の 126）。**v1 の成果物は読まずに例外にする。**実測：成果物は 5.3 → **3.1 MB**（節 75,900 × 2 ターゲット）。** **v1.21（2026-09-10）モデルの登録簿を追加した（W4 の PR E、migration 0038）。§4.14 に **`model_versions`**。**「いま何を配っているか」の正が DB になった**（環境変数 `BASELINE_MODEL_VERSION` は使わない）。`active` は同時に 1 つだけで、**昇格の関数には誰にも grant していない**（PostgREST 越しには 403）。**`feature_set` の照合は種類で違う**：LightGBM は 62 列すべてを読むので不一致なら配らず、ベースラインは読む 6 列が v0 から v3 まで変わっていないので配る（W4-17）。** **v1.20（2026-09-10）天気を実装した（W4 の PR D、migration 0037）。§4.13 に **`weather_hourly`**（1 行 = 1 発行 × 1 格子、時間帯は配列）を追加。§7.4 の `feature_set` を **v3** にし、**天気の 4 列**（`precip_mm_now` / `precip_mm_target` / `temp_c` / `wind_kmh`）を足した（**列は 65 → 69**、特徴量は 57 → 61）。§8.4 に「**要求した格子と返ってくる格子が 2 件だけ食い違う**」（ポート 4 件が恒久的に NULL）、§8.5 を「まだ決まっていないこと」から「**変数ごとに『その時刻が何を指すか』が違う**」に書き換えた（降水量は直前 1 時間の合計、気温と風速は瞬時値、風速の単位は **km/h**）。**アーカイブ開始前（2026-09-07 15:17 UTC）は天気列を NULL のままにする**（バックフィルはしない）。** **v1.19（2026-09-10）§7.4 の `feature_set` を v2 にした。**`minutes_since_last_change` に上限（180 分）を入れた**（W4 プラン §4 の W4-10）。この列だけが読み込んだ窓の長さで値が変わり、**25 時間読む学習と 3 時間読む推論で必ず食い違っていた**。上限を決めて、どちらも同じ値を出せるようにした。あわせて**推論も同じ 57 列を作る**ようになり（W4 の PR C）、学習の 1 点との一致をゴールデンで固定している。**v1 と v2 を混ぜて学習しない。** 配信中のベースライン（B0〜B3）はこの列を使っていないので、いま出ている予測は変わらない。** **v1.18（2026-09-09）§5.1 の `capacity` を「**固定のラック数だけ**」に変えた（0035）。動的な系統（ドコモ）は NULL を返す。日次同期の瞬間の `bikes + docks` が凍結された値をそのまま渡していたため、**628 ポート（10.8%）で「容量 5・借りられる 12」という矛盾**が画面に出ていた（W4 プラン §12 の 115）。**基底テーブルの値は消していない**（学習側はそちらを読む）。** **v1.17（2026-09-09）§5.1 に `forecast_generated_at` を足した（0034）。**予測の時刻は 2 つあり、役目が違う**：`base_observed_at` は鮮度の判定（どの観測に基づくか）、`generated_at` は**水平の起点**（`p_*_x1000[i]` は「これ ＋ `horizons_min[i]` 分後」）。取り違えると、利用者の到着より早い時刻の確率を返す（W4 プラン §12 の 114）。** **v1.16（2026-09-09）§5 に予測の列を足した（0033）。`v1_stations_current` は `station_forecasts` を `left join` し、**予測が無いポートも行を返して 6 列を NULL にする**。**鮮度はビューで切らない**（`FORECAST_STALE_AFTER_S` は TypeScript が正）。`v1_feeds` は `inference_log` の**最新の `ok`** から「いま配信している版」を返す。実測で予測の付くポートは **99.15% / 99.59%**。pgTAP は 26 → 38 項目で、列の一覧も `columns_are` で固定した。** **v1.15（2026-09-09）§4.12 に「閉じられなかった `running` を日次の保守が `failed` にする」（0032）を書いた。`error = 'never_finished'` で「失敗した」と区別する。** **v1.14（2026-09-09）§4.12 に `inference_log` の保持（**90 日、`ok` のみ**。0031）と `detail.cpu_ms`（列にせず `detail` に入れる。W3 プラン §14.7）を書いた。** **v1.13（2026-09-09）§4.12 に `inference_log.detail`（0030）を足した。**成果物に無かったポートの数（`unknown_ports`）が 5 分毎に出る**ようになり、再学習の間隔が長すぎることに気づけるようになった（W3 プラン §12 の 110）。列に在るものは `detail` に入れない。** **v1.12（2026-09-09）§7.4 の `feature_set` を v1 にした。参照データを日次スナップショット（`reference/date=YYYY-MM-DD/`）から読むようになり、`capacity` は**前日までの 7 日の `max(bikes + docks)`**、近傍と静的属性は**前日の版**で固定される。**v0 と v1 を混ぜて学習しない**（W3 プラン §14.3・§14.6）。** **v1.11（2026-09-09）§4.11 にドコモの推論を足した（`1-59/5`。HELLO の `4-59/5` と 3 分ずらす）。行数の実測は HELLO のみのもので、両系で約 20,400 行・10 MB の見込み。** **v1.10（2026-09-09）§4.11 を本番稼働の実測に更新した。5 分毎に更新されるのは「全件」ではなく、貸出可・返却可の両方が立つポートだけ（HELLO で 96.7%）で、止まったポートの古い行が残る。読む側は `generated_at` の鮮度で切ること（W3 プラン §12 の 109）。** **v1.9（2026-09-08）§4.7 に `job_runs` の所要の読み方を足した。`finished_at` を `clock_timestamp()` に直した（0028）ので、`started_at`（トランザクション開始）との差が所要になる。0028 より前の行は pg_cron の 7 系統がすべて 0 秒で、前後を比べてはいけない（W3 プラン §12 の 94）。** **v1.8（2026-09-08）予測テーブル（§4.11）と推論の記録（§4.12）を追加。** **v1.7（2026-09-08）学習サンプル（`features/`）のパスと 65 列の契約を追加（§7.4）。読む側の規則に「ファイル自身の列を確かめる」を足した（§7.3）。** **v1.6（2026-09-08）行政区画コードと近傍リストを追加（§4.9・§4.10・§4.2）。** **v1.5（2026-09-08）暦（§7.5）と天気ファイルの入手時刻（§7.6）を追加。** **v1.4（2026-09-08）Parquet に `fetched_at` を足し、全期間を畳み直した（§7.3）。読む側は必ず明示スキーマを渡す規約を書いた。** **v1.3（2026-09-08）`OPEN_METEO_FORECAST_DAYS` を 2 → 3 に広げた（§7.2・§8.1・§8.2）。2 モデルの食い違いが緯度で決まることを 371,280 値で測り直した（§8.3）。** v1.2（2026-09-08）W2 完了時点の点検を反映（`job_runs` の保持規則の記述を訂正、天気ファイルの入手時刻、`fetched_at` の代用）。v1.1（2026-09-08）EDA #1 の結果を反映（実在しないポート `5753`、名前での除外が危険なこと）。v1.0（2026-09-08）初版。W2 の PR F。

---

## 1. どこから読むか

| 用途 | 読む先 | 形 | 保持 |
|---|---|---|---|
| **学習** | Storage `gbfs-parquet` | 長形式 Parquet（1 行 = 1 ポート 1 観測） | 無期限 |
| **推論（オンライン）** | Postgres `status_snapshots` / `station_status_latest` | 配列（1 行 = 1 フィード更新） | **60 日** |
| **配信** | Postgres のビュー `v1_stations_current` / `v1_feeds` | 1 行 = 1 ポート | — |
| **原典・作り直し** | Storage `gbfs-raw` | GBFS の生 JSON（gzip） | 無期限 |
| **天気** | Storage `weather-raw` | Open-Meteo の応答そのまま（gzip） | 無期限 |
| **配った予測** | Storage `forecast-log` | Parquet（1 行 = 1 ポート、確率は配列） | **12 か月** |

**一次ソースは生 JSON である。** Postgres の配列も Parquet も、そこから作り直せる派生物にすぎない（開発プラン D-03）。逆に言うと、**生 JSON に無いものはどこにも無い。**

**Postgres は 60 日で消える。** `status_snapshots` は月次パーティションで、保守ジョブが保持期間の外に出た月を落とす。9 月分は **2026-11-30** 以降に消える。それより古い期間を学習で使うときは Parquet か生 JSON を読む。

---

## 2. 読む前に必ず知っておく 9 つの約束

**この節を飛ばすと、静かに間違った特徴量ができる。**

### (1) 時刻は 3 種類あり、混同すると意味が変わる

| 列 | 意味 | 使いどころ |
|---|---|---|
| `observed_at` | **フィードの `last_updated`**。事業者が値を確定した時刻 | 時系列の軸。学習の `t` はこれで並べる |
| `fetched_at` | **当方が取得を完了した時刻** | 取得の健全性を見るとき。`fetched_at − observed_at` が公開遅延 |
| `last_changed_at` | **値が最後に「変わった」時刻**（`station_status_latest` のみ） | 「いつから同じ値か」を知りたいとき。**「最後に観測した時刻」ではない** |

すべて `timestamptz`（Postgres は UTC で保存）。**唯一の例外が天気の `hourly.time` で、これは JST のナイーブ文字列**（§8）。

### (2) 配列は `arr[idx + 1]` で引く

`status_snapshots` の 4 本の配列は `stations.idx` の順に並ぶ。Postgres の配列は 1 起点なので、`idx` のポートの値は `arr[idx + 1]` にある。`idx` はシステム内で**一意・不変・0 起点・密**。

### (3) `-1` は「観測されなかった」。0 ではない

登録済みのポートがそのフィードに現れなかったときの値。**`0`（本当に 0 台）と必ず区別する。**

### (4) `idx >= array_length` は「その時点でまだ台帳に無い」

配列の外側は「未登録」を意味する。**ゼロ埋め禁止。** 0 と読むと、新しいポートが登録される前の期間ずっと「空のポート」として学習される。台帳は 1 日に **HELLO +87 / ドコモ +10**（2026-09-07 の実測）増えており、60 日では無視できない。

### (5) 配列長は時系列で単調に増えるとは限らない

生 JSON から再構築した行の配列長は、**観測時点ではなく再構築した時点の台帳**で決まる。実測で「最も古い行が最も長い配列を持つ」逆転が起きている。判定は必ず**その行の `array_length`** に対して行い、「古い＝短い」を前提にしない。

### (6) 負の台数・返却枠は 0 に丸めてある

HELLO は定員超過のポートで `num_docks_available` に `-1` を返す（1 スナップショットあたり中央値 10 件）。`-1` は欠損専用の値なので、**取り込み時に 0 へ丸めている**。意味の上でも正しい（定員を超えて停まっているポートに返す枠は無い）。**原文が要るなら生 JSON を読む。**

### (7) 値の違う重複 `station_id` は先頭を残して捨てている

ドコモで累計 5 件。捨てたほうは生 JSON にだけ残る。

### (8) 「現在の値」の列を過去のサンプルに使わない

`stations.is_active`、`stations.last_seen_at`、`station_attributes` の現在有効な行は、いずれも**いま**の状態である。過去の時点の判定に使うと**生存者バイアス**が入る（§10.1）。

### (9) `flags` は実質 1 ビットしか持っていない

**蓄積している全期間**（2,329 スナップショット、2026-09-06〜09-08）で `flags` が取った値は **7 / 1 / −1 の 3 つだけ**。`3`（貸出可・返却不可）と `5`（返却可・貸出不可）は**一度も現れていない**。つまり `is_renting` と `is_returning` は常に一致し、`is_installed` は常に真である。

| `flags` | 意味 | HELLO | ドコモ |
|---|---|---|---|
| `7` | 設置・貸出可・返却可 | 97.445% | 99.401% |
| `1` | 設置のみ（**貸出も返却も不可**） | 2.553% | 0.410% |
| `−1` | 観測されなかった | 0.002% | 0.189% |

（2026-09-07 の 1 日、全ポート × 全スナップショットを展開して算出）

**含意**：「借りられるか」と「返せるか」の違いは `flags` からは出てこない。**台数（`bikes` / `docks`）だけが両者を分ける。** `flags` は「運用停止か否か」の 1 ビットとして扱う。

**ただし観測期間は 2 日である。** 事業者が「貸出のみ停止」を使う運用をしていないとまでは言い切れない。**`3` / `5` を受け取っても壊れないローダーを書き**、期間が伸びたら測り直す。

---

## 3. 一次ソース：GBFS の生フィード

ODPT の GBFS v2.3。収集しているのは 2 フィードだけで、いずれも `ttl: 60` / `version: "2.3"` を返す。

| フィード | 周期 | 収集 | 保存先 |
|---|---|---|---|
| `station_status.json` | HELLO 300 秒 / ドコモ 81 秒 | **毎分**（Vercel Cron） | `gbfs-raw` ＋ `status_snapshots` |
| `station_information.json` | 日次で十分 | **1 日 1 回**（04:00 JST） | `gbfs-raw` ＋ `station_attributes` |

**収集していない GBFS フィード**：`system_information` / `vehicle_types` / `system_regions` / `system_pricing_plans` など。ドコモの `region_id` に対応する地域名は `system_regions.json` にあるが、**いまは取っていないので数値 ID しか無い**（§12）。

### 3.1 `station_status` に実在するフィールド

| フィールド | HELLO | ドコモ | 型 | 備考 |
|---|---|---|---|---|
| `station_id` | ○ | ○ | string | |
| `num_bikes_available` | ○ | ○ | int | → `bikes` |
| `num_docks_available` | ○ | ○ | int | → `docks`。**HELLO は負値あり**（0 に丸める） |
| `is_installed` | ○ | ○ | bool | **全件 true**（実測。2 スナップショットで 14,835 / 14,921 件すべて） |
| `is_renting` | ○ | ○ | bool | → `flags` の bit 2 |
| `is_returning` | ○ | ○ | bool | → `flags` の bit 4 |
| `last_reported` | ○ | ○ | int（epoch 秒） | → `reported_age_s`。**ドコモは全件 `last_updated` と同値** |
| `vehicle_types_available` | ○ | — | list | **下記** |
| `vehicle_docks_available` | ○ | — | list | **下記** |

**`vehicle_types_available` / `vehicle_docks_available` は、いまのところ完全に冗長である。**

```json
"vehicle_types_available": [{"count": 8, "vehicle_type_id": "2"}]
"vehicle_docks_available": [{"count": 0, "vehicle_type_ids": ["2"]}]
```

実測（最古 2026-09-06 と最新 2026-09-08 の 2 スナップショット、計 29,756 ポート）で、

- 要素数は**常に 1**、`vehicle_type_id` は**常に `"2"`**
- `count` の合計は `num_bikes_available` / `num_docks_available` と**全件一致**（不一致 0 件）

したがって **`bikes` / `docks` 以上の情報を持たない。生 JSON を読み直す価値は現時点では無い。** 事業者が車種を増やしたら（電動と非電動を分けるなど）意味を持つので、W4 以降に再確認する。**検査したのは 2 スナップショットだけ**なので、使う前にもう一度確かめる。`vehicle_type_id: "2"` が何を指すかは `vehicle_types.json` にあるが未収集。

### 3.2 `station_information` に実在するフィールド

出現率は現在有効な属性行に対する割合（HELLO 14,922 / ドコモ 5,820）。

| フィールド | HELLO | ドコモ | 型 | 特徴量としての価値 |
|---|---|---|---|---|
| `station_id` / `name` / `lat` / `lon` | 100% | 100% | string / string / number / number | 座標は地理特徴の素 |
| `vehicle_capacity` | 100% | — | **string**（`"5"` のような数字文字列） | HELLO の実容量。`capacity` 列に数値化して入れてある |
| `capacity` | — | 100% | number | **固定ラック数ではない。§4.3 を必ず読む** |
| `is_charging_station` | 100% | — | bool | **使える**。true は 271 / 14,922（**1.8%**） |
| `region_id` | — | 100% | string | **使える**。1〜15+ のエリア ID。最大の `1` に 2,989 / 5,820（51%）が属する |
| `address` | 100% | — | string | ドコモに無いので両システム共通の特徴にはできない |
| `contact_phone` | 100% | — | string | 使わない |
| `rental_uris` | 100% | — | object（`ios` / `android` / `web`） | 事業者アプリへのディープリンク。**UI 用**であって特徴量ではない |
| `parking_type` | 100% | — | string | **分散ゼロ。全件 `street_parking`。特徴量にならない** |
| `parking_hoop` | 100% | — | bool | **分散ゼロ。全件 `false`。特徴量にならない** |

> 開発プラン §6.7 は `parking_type` / `parking_hoop` を「ラックの形式は返却可否の挙動に効く可能性があり、W4 で効果を見る」としていたが、**実測で両方とも定数だったので見る必要は無い。**

### 3.3 フィードの周期と遅れ（実測）

| 項目 | HELLO | ドコモ | 学習への含意 |
|---|---|---|---|
| 更新周期 | 300 秒（p95 302・最大 305） | 81 秒（p95 83・最大 186） | **HELLO は水平 5 分がラベルの分解能の限界** |
| 公開遅延 `fetched_at − observed_at` | 中央値 67 秒・最大 226 秒 | 中央値 31 秒・最大 77 秒 | 除外規則の「10 分」には十分な余裕 |
| `reported_age_s` | 中央値 21 秒・p95 39 秒 | **恒常的に 0** | ドコモの 0 は「常に新鮮」ではなく**「情報が無い」**。0 と欠損を区別する |

---

## 4. Postgres のテーブル

**書き手を列ごとに固定してある。** 「誰が書くか」を混ぜないことで、「Storage には残ったが DB に入らなかった」という静かな取りこぼしを構造的に防いでいる。表の「書き手」列はその約束である。

### 4.1 `systems` — 事業者システム（2 行）

| 列 | 型 | 意味 |
|---|---|---|
| `system_id` | text PK | `hellocycling` / `docomo-cycle` |
| `display_name` / `operator_name` | text | 表示名・クレジット表記用 |
| `gbfs_base_url` | text | **参照用。収集器は読まない**（URL の組み立ては `odpt-fetch.ts` だけ） |
| `expected_cadence_s` | int | 実測の更新周期。HELLO 300 / ドコモ 81 |
| `poll_interval_s` | int | こちらが取りに行く間隔。両方 60 |
| `lock_key` | smallint | アドバイザリロックの第 2 引数。`hashtext()` に依存しないため明示列にしてある |
| `is_active` | bool | 収集中か |
| `capacity_is_dynamic` | bool | `capacity` が動的値か。**ドコモ true / HELLO false** |

### 4.2 `stations` — ポート台帳（行は削除しない）

| 列 | 型 | 意味 | 書き手 |
|---|---|---|---|
| `system_id, station_id` | text | PK | |
| `idx` | int | **一意・不変・0 起点・密**。配列の位置 | `ingest_snapshot`（採番のみ） |
| `first_seen_at` | timestamptz | 台帳に載った時刻 | 同上 |
| `last_seen_at` | timestamptz | 直近 25 時間から計算 | 日次 `refresh_station_activity` |
| `is_active` | bool | 72 時間観測されなければ false | 同上 |
| `pref_code` / `muni_code` | smallint / int | 行政区画コード（JIS X 0401 / X 0402）。**住所から** `muni_codes` への前方一致で導く。ドコモは住所が無いので**両方 NULL** | 日次 `rebuild_geo`（§4.9） |

**`is_active` と `last_seen_at` は「いまの状態」である。** 過去のサンプルの判定に使ってはいけない（§10.1）。

### 4.3 `station_attributes` — 属性の履歴（SCD Type 2）

| 列 | 型 | 意味 |
|---|---|---|
| `system_id, station_id, valid_from` | | PK |
| `valid_to` | timestamptz | **現在有効な行は NULL**。値が変わると旧行を閉じて新行を足す |
| `name` / `lat` / `lon` | text / float8 | 属性 |
| `capacity` | smallint | **意味がシステムで違う。下記** |
| `geo_suspect` | bool | 日本の外接矩形（lat 20–46 / lon 122–154）の外 |
| `raw` | jsonb | **GBFS オブジェクト全体**。未知フィールドの保全先 |

**ある時点の属性を引くには `valid_from <= t and (valid_to is null or valid_to > t)`。** 現在有効な行だけを取るなら `valid_to is null`（部分ユニーク索引があるので 1 行に定まる）。

**`capacity` の意味はシステムで違う。**

| | HELLO | ドコモ |
|---|---|---|
| 出どころ | 非標準の `vehicle_capacity`（文字列）を数値化 | 標準の `capacity` |
| 意味 | **固定の定員** | **`bikes + docks` の動的値**（`capacity_is_dynamic = true`） |
| 使ってよいか | ○ | **✗（そのままでは使えない）** |

ドコモの `capacity` は日次同期の瞬間の値で凍結される（SCD2 の比較から外してあるため、変化しても新しい版を作らない）。同期の瞬間ですら `bikes + docks` と**完全一致するのは 49.7%** しかない（±1 で 72.8%、±3 で 86.7%、平均差 −0.26、範囲 −46〜+161。2026-09-08 04:00 JST 前後で実測）。`station_information` と `station_status` の生成時刻が数分ずれるためである。

→ **ドコモでは `capacity` を「ラック数」として使わない。`gap` も計算しない。** 容量が要るなら `max(bikes + docks)` を期間内で推定する（開発プラン §6.7）。

**`geo_suspect`**：実測 1 件。ドコモの `4826`「アートフォーラムあざみ野」の経度が `39.553764`（正しくは `139.553764`）。**時系列と予測は保持し、地図（`/v1/stations`）にだけ出さない。** 提供側が直したら値が変わる。

**版はほとんど増えない。** 実測で全 20,749 行のうち閉じた行は **7 行**だけ。動的な `capacity` を比較から外していなければ 2 分で 154 版増えていた。

### 4.4 `status_snapshots` — 配列スナップショット（1 フィード更新 = 1 行）

月次 RANGE パーティション（`observed_at`）＋ DEFAULT パーティション。**保持 60 日。**

| 列 | 型 | 意味 |
|---|---|---|
| `system_id, observed_at` | | PK。`observed_at` はフィードの `last_updated` |
| `fetched_at` | timestamptz | 取得を完了した時刻 |
| `n_stations` | int | **そのスナップショットに現れたポート数**。配列長とは違う |
| `is_anomalous` | bool | 出現ポート数が登録済みの 50% 未満だった（実測 0 件） |
| `bikes` / `docks` / `flags` / `reported_age_s` | smallint[] | `idx` 順。**欠損は −1** |
| `raw_path` | text | 生 gzip JSON への参照 |

- `flags` はビット和：`1 = is_installed` / `2 = is_renting` / `4 = is_returning`。**実際に現れるのは 7 / 1 / −1 だけ**（§2 の (9)）
- `reported_age_s` は `observed_at − last_reported`。負値は 0 に丸める（`−1` は欠損専用なので衝突させない）
- **`n_stations` ≠ `array_length`。** 前者は「今回現れた数」、後者は「取り込み時点の登録済み数」

### 4.5 `station_status_latest` — 最新状態（1 ポート 1 行）

**変化した行だけを更新する。** `fillfactor = 70`。

| 列 | 型 | 意味 |
|---|---|---|
| `bikes` / `docks` / `flags` | smallint | 最新値。**一度も観測されていないポートは −1**（実測 2 件） |
| `is_present` | bool | 最新スナップショットに現れたか。**消えても値は保持する** |
| `last_changed_at` | timestamptz | 上の 4 つのいずれかが**最後に変わった**時刻 |

**「フィード全体の鮮度」はここから取れない。** `last_changed_at` は値が変わった時刻なので、変化の無いポートでは古い。鮮度は `feed_state.last_observed_at` を見る。

### 4.6 `feed_state` — フィードの状態（1 システム 1 行）

**列ごとに書き手を固定してある。**

| 列 | 書き手 | 意味 |
|---|---|---|
| `last_fetch_at` | `begin_fetch` | 二重起動の抑止とウォッチドッグの判定 |
| `last_success_at` | `finish_fetch` | ODPT から正常な応答を得た最後の時刻 |
| `last_observed_at` | **`ingest_snapshot` だけ** | 取り込みに成功した最後の `observed_at`。**前進のみ**（後退しない） |
| `last_etag` | **`ingest_snapshot` だけ** | 条件付き要求に使う。取りこぼし防止の要 |
| `consecutive_errors` | `finish_fetch` | 連続失敗数 |

### 4.7 運用のログ（学習には使わないが、品質判定に要る）

| テーブル | 何が入るか | 学習での使い道 |
|---|---|---|
| `feed_fetch_log` | 取得 1 回 1 行。`result` は `inserted` / `duplicate` / `unchanged` / `skipped_recent` / `locked` / `error`。**URL とトークンは入らない**（`endpoint` は `token` / `public` のみ） | 欠測区間の特定 |
| `job_runs` | ジョブ 1 回 1 行（`detail` は jsonb）。**保持規則が無く、消えない**（`run_maintenance` が消すのは `feed_fetch_log` の 30 日と `cron.job_run_details` の 7 日だけ）。1 日約 1,780 行増え、うち 1,441 行は「何も起きていない」ウォッチドッグ | 障害調査。**天気アーカイブが動いた証拠**もここにしか無い |
| `daily_quality` | **JST の日付**ごと・システムごとの品質。`n_snapshots` / `n_expected` / `max_gap_s` / `n_anomalous` / `db_bytes_delta` | **学習期間の品質フィルタに使える** |
| `alert_state` | 通知の抑制状態 | 使わない |
| `app_config` | `collect_interval_s` などの運用設定 | 使わない |

**`daily_quality.quality_date` は JST の日付である**（Storage のパスと Parquet のパーティションは UTC）。突き合わせるときに 9 時間ずれる。

**`job_runs` の所要は `finished_at - started_at` で出す。** `started_at` はトランザクション開始（`now()`）、`finished_at` は実時刻（`clock_timestamp()`）。**0028 より前の行は、pg_cron から呼ぶ 7 系統がすべて 0 秒になっている**（`now()` 同士で、同じトランザクションでは動かなかった。W3 プラン §12 の 94）。**適用の前後を並べて比べてはいけない。** `trigger_backup_collect` の所要は例外で、`net.http_post` の投函までしか測れない（バックアップの実所要は `backup_collect:<system>` の行にある）。

---

### 4.8 `jp_holidays` — 祝日（内閣府 CSV そのもの）

```sql
create table public.jp_holidays (holiday_date date primary key, name text not null);
```

- 中身は**内閣府「国民の祝日について」CSV そのもの**。1,067 件・**1955-01-01 〜 2027-11-23**（実測 2026-09-08）
- `name` は「元日」などの祝日名と、振替休日・国民の休日を表す**「休日」**。名称で区別できる
- **年末年始（12/29〜1/3）とお盆（8/13〜16）は入っていない。** 暦の規則なので `packages/shared/src/calendar.ts` と `apps/ml/bikechance_ml/features/calendar.py` が持つ
- 入れ替えは `replace_jp_holidays(jsonb)` の 1 トランザクション。**100 行未満は受け付けない**（取得の失敗で表を空にしない）
- 取り込みは `scripts/import-holidays.ts`。**条件付き取得は効かない**ので中身の SHA-256 で差分を見る

**`day_type` は 7 値、`dow_type` は 3 値。**

| `day_type` | 優先 | 条件 | `dow_type` |
|---|---|---|---|
| `newyear` | 1 | 12/29〜1/3 | `sun_holiday` |
| `obon` | 2 | 8/13〜16 | `sun_holiday` |
| `holiday` | 3 | `jp_holidays` に在る | `sun_holiday` |
| `sun` | 4 | 日曜 | `sun_holiday` |
| `sat` | 5 | 土曜 | `sat` |
| `bridge` | 6 | 前日も翌日も休みの平日 | `weekday` |
| `weekday` | 7 | それ以外 | `weekday` |

> **`newyear` / `obon` が祝日より優先する。** 元日は法定の祝日でもあるが、期間全体が休業として振る舞うため。**2026 年の `holiday` は 17 件**（CSV は 18 件だが元日が `newyear` に吸われる）。

> **プロファイルと気候値（B2）のセルは `dow_type`（3 値）で切る。** 7 値で切るとセルあたりのサンプルが半分以下になり、`station × dow_type × slot15` が埋まらない。

**2 言語の実装は `fixtures/calendar/day_type_golden.csv`（730 日）で突き合わせてある。** 規則を変えたら `pnpm exec tsx scripts/gen-calendar-golden.ts` で作り直し、**差分をレビューする**。

### 4.9 `muni_codes` — 全国地方公共団体コード（1,917 行）

```sql
create table public.muni_codes (
  muni_code integer primary key,   -- 上 5 桁。検査数字は含まない
  pref_code smallint not null,     -- muni_code / 1000
  pref_name text not null,         -- 「神奈川県」
  muni_name text not null          -- 「横浜市瀬谷区」（政令市の区も 1 行）
);
```

- 出典は総務省「全国地方公共団体コード」。原本は `supabase/seed/muni_codes.csv`、生成は `scripts/gen-muni-codes.py`
- **`smallint` に入らない。** 沖縄県与那国町は 47382 で上限 32767 を超える
- **住所の解析はこの表への前方一致で行う。正規表現を使わない。** 「蒲郡市」「東村山市」「廿日市市」は正規表現だと必ず間違える（W3 プラン §12 の 91）

### 4.10 `station_neighbors` — 半径 500 m の近傍（106,438 行）

```sql
create table public.station_neighbors (
  system_id text, station_id text, nb_system_id text, nb_station_id text,
  distance_m  smallint,   -- 0〜500。四捨五入した整数メートル
  same_system boolean,    -- system_id = nb_system_id
  primary key (system_id, station_id, nb_system_id, nb_station_id)
);
```

- **片方向の行が両方入る**（A→B と B→A）。片側だけ読めばよい
- **300 m の特徴量は `distance_m <= 300` で絞る。** 表を 2 つ持たない
- `same_system` は事業者が同じかどうか。**HELLO とドコモは別事業者で、利用者はふつう乗り換えられない**ので、近傍の集約は「同一システムのみ」と「全部」の 2 系統を作る
- `geo_suspect`（座標が日本の外）のポートは入らない。**外部キーを張っていない**（毎日全置換するので孤児が生じない）

**近傍が 1 つも無いポートは実データである。欠損ではない。**

| 半径 | ペア数 | 平均 | 中央 | p90 | 近傍 0 のポート |
|---|---|---|---|---|---|
| 300 m | 42,950 | 2.1 | 1 | 5 | **5,776（27.8%）** |
| 500 m | 106,438 | 5.1 | 4 | 11 | 2,111（10.2%） |

`nb_bikes_sum` のような合計は **0 で埋める**。`nb_fill_ratio_mean` のような平均は分母が 0 なので **NULL にする**。1 km 以内に 1 件も無いのは HELLO 373 件・ドコモ 127 件で、そこだけ `muni_code` の平均に落とす（HELLO のみ。ドコモは市区町村が無いのでシステム全体の平均）。

**更新は日次の `rebuild_geo()`**（pg_cron `30 19 * * *` UTC ＝ 04:30 JST、属性同期の後）。`rebuild_station_geo()` で `stations.pref_code` / `muni_code` を埋め、`rebuild_station_neighbors()` で近傍を**全置換**する。結果は `job_runs` の `detail` に `{geo: {...}, neighbors: {...}}` で残る。**当たらなかった住所は推測で埋めず、件数を `detail.geo.unmatched` に出す。**

### 4.11 `station_forecasts` — 先回り予測（HELLO 実測 14,635 行 ＋ ドコモ見込み 5,768 行）

```sql
create table public.station_forecasts (
  system_id text, station_id text,          -- PK。**1 ポート 1 行**
  generated_at timestamptz,                 -- 計算した時刻
  base_observed_at timestamptz,             -- **予測の基準になった観測時刻。鮮度はこれで見る**
  model_version text,
  horizons_min smallint[],                  -- {5,10,15,20,30,45,60,90,120,180}
  p_bike_x1000 smallint[],                  -- P(bikes >= 1) × 1000
  p_dock_x1000 smallint[],                  -- P(docks >= 1) × 1000
  confidence smallint                       -- 0〜3
) with (fillfactor = 50);
```

- **確率は ×1000 の整数。** 倍精度だと 3.3 MB、smallint なら 830 kB。表示は 10% 刻みなので分解能は足りる
- **水平は配列。** 1 行 1 水平にすると 20 万行を 5 分毎に書き換えることになる
- 5 分毎に UPSERT（`fillfactor = 50` で HOT 更新に乗せる）。**本番 11 時間の運転で HOT 率 100.0%**（W3 プラン §5.10）
- **本番稼働中。** HELLO は 2026-09-08 20:24 JST から（`4-59/5`）、**ドコモは 2026-09-09 から**（`1-59/5`。3 分ずらして重ねない）。**14,635 行・7.2 MB** は HELLO だけを 11 時間 5 分・134 サイクル動かした 2026-09-09 07:29 の実測で、ドコモを足すと約 20,400 行・10 MB になる見込み
- **「全件」ではない。1 周期で更新されるのは `flags` に貸出可・返却可の両方が立つポートだけ**（HELLO で 96.7%）。止まっているポート（3.3%）は**出さない**ので、以前の行が古いまま残る。実測で 199 行・最古 11 時間前
- **読む側は必ず `generated_at`（または `base_observed_at`）の鮮度で切る。** 切らないと、止まったポートの 11 時間前の確率を「現在の予測」として配ることになる（W3 プラン §12 の 109）

**`confidence` の意味**（W3 プラン §12 の 105）

| 値 | 意味 |
|---|---|
| 1 | フィードが古い（`t − base_observed_at > 600 秒`） |
| 2 | 参照表の再校正だけ。**そのポートの履歴が足りていない** |
| 3 | **過半の水平で気候値が効いた**（そのポート・曜日種別・時刻の実績がある） |

**2026-09-13（W5 の PR D）から 2 の意味が絞られた。** 気候値をポートプロファイルから作るようになり、引けない理由が「**そのセルに寄与した日が 2 日に満たない**」だけになった（それまでは抽出が薄く**全ポートが 2** だった）。**列コメント（migration 0026）は W3 段 8 のときのもの**で「気候値が無いので最大 2」と書いてある——**正はこの表**であり、コメントは配信を入れ替えるときに直す。

> **行が無いポートがある。** 予測を出せないポート（最新スナップショットに現れていない・値が `-1`・運用停止・実在しないポート）は**行を作らない**。学習と同じ除外規則で、0 や 0.5 で埋めない。既にある行は `generated_at` が古いまま残るので、**鮮度で「予測なし」を判断する**。

### 4.12 `inference_log` — 推論 1 回ぶんの記録

```sql
create table public.inference_log (
  id bigint, system_id text, generated_at timestamptz, finished_at timestamptz,
  base_observed_at timestamptz, model_version text,
  status text,        -- 'running' | 'ok' | 'failed' | 'skipped'
  n_rows integer, duration_ms integer, error text, detail jsonb,
  unique (system_id, base_observed_at)
);
```

- **`(system_id, base_observed_at)` の一意制約が二重推論を止める。** 行を入れられたら掴めた、入れられなければ誰かがやっている。**アドバイザリロックは使わない**（PostgREST 越しには効かない。W3 プラン §12 の 103）
- `finished_at` は `clock_timestamp()`（`now()` はトランザクション開始時刻で動かない。§12 の 94）
- **`detail jsonb`（0030）には、列になっていない 4 つだけ**を入れる：`stations`（台帳のポート数）・`skipped`（予測を出さなかった数）・**`unknown_ports`（成果物に無かったポートの数）**・**`cpu_ms`（そのプロセスが使った CPU 時間）**。`status` などは列で持っているので重複させない
- **`cpu_ms` は列にしない。** 開発プラン §5.3 の DDL には `cpu_ms integer` が在るが、実装は `detail` に入れる（0030 で口ができたので、列を足す必要が無くなった。W3 プラン §14.7）。**費用は実時間ではなく Active CPU で決まる**ので、`duration_ms`（Storage の取得と往復を含む）とは分けて見る
- **保持は 90 日、ただし `ok` だけ**（0031）。失敗・二重抑止・閉じられなかった行は消さない。**5.2 万行・約 31 MB で頭打ち**になる
- **`running` のまま 1 時間を超えた行は、日次の保守が `failed`（`error = 'never_finished'`）にして閉じる**（0032）。「失敗した」（推論が例外で落ちた）と「閉じられなかった」（`finish_inference` に届かなかった）は**別のこと**なので、`error` で区別する
- **`unknown_ports` が数百に育ったら、再学習の間隔が長すぎる。** そのポートは気候値が引けず B1 だけになる（W3 プラン §12 の 110）
- **`job_runs` とは別の表。** ジョブの監視（`monitored_jobs`）にはまだ入れていない

### 4.13 `weather_hourly` — 予報（1 行 = 1 発行 × 1 格子）

```sql
create table public.weather_hourly (
  cell_lat_idx smallint,   -- round(lat / 0.05)
  cell_lon_idx smallint,   -- round(lon / 0.0625)
  issued_hour  timestamptz,   -- 取得を始めた時刻を「時」に丸めた値（パスの一部）
  available_at timestamptz,   -- **入手できた時刻。特徴量はこれで引く**
  precip_mm real[], temp_c real[], wind_kmh real[], weather_code smallint[],
  primary key (cell_lat_idx, cell_lon_idx, issued_hour)
);
```

**`weather-raw` の gzip JSON（§7.2）から作る派生物。** 一次ソースは Storage のほうで、この表は
いつでも作り直せる（取り込みは `/ml/weather`、毎時 :25 UTC）。

- **配列の添字 `k` は `issued_hour + k 時間`**（SQL は 1 始まりなので `precip_mm[k+1]`）。長さは 8
  （`WEATHER_LEAD_HOURS`）。発行が 2 回続けて落ちても穴が開かない長さにしてある
- **`jma_msm` の値だけ**が入る（W4-05）。`best_match` は同じファイルに在るが取り込まない
- **`precip_mm` は「直前 1 時間の合計」、他は瞬時値**（§8.5）。同じ添字でも指す区間が違う
- **降水確率の列は無い。** `jma_msm` は返さない（210,630 値すべて null）
- **`weather_code` は入っているが特徴量には使っていない**（降水量とほぼ一対一。独自なのは
  「雨が降っていないときの雲量」だけで、それを使う根拠がまだ無い）
- **保持は 30 日**（`run_maintenance`）。**5.2 MB/日**・372 B/行（実測）で、30 日で約 152 MB
- **未処理は `v_weather_pending` に出る**（`ok` で保存されたのに、まだ入り切っていない発行）。
  読む側は `available_at` の下限を必ず添える（保持で消えた発行が蘇らないように）

> **⚠️ `issued_hour <= t` で引いてはいけない。** 引くのは **`available_at <= t`** である（§8.6）。

### 4.14 `model_versions` — モデルの登録簿（0038）

```sql
create table public.model_versions (
  model_version text primary key,   -- 'baseline-b3-v0-20260908' / 'lgbm-v0-20260907'
  kind          text,               -- 'baseline' | 'lightgbm'（**成果物の読み方**）
  status        text,               -- 'candidate' | 'shadow' | 'active' | 'retired'
  feature_set   text,               -- **その版を当てはめたときの**特徴量の版
  artifact_path text,               -- models バケット内のパス
  train_days    text[], metrics jsonb, card_path text, note text,
  created_at timestamptz, promoted_at timestamptz
);
```

**「いま何を配っているか」の正はこの表**である（W3 の段 8 では環境変数だった）。

- **`active` は同時に 1 つだけ**（部分一意索引）。`shadow` も 1 つだけ
- **昇格は `promote_model_version()` だけ。** この関数には**誰にも grant していない**ので、
  所有者が psql から呼ぶ（CLAUDE.md §6。PostgREST 越しには 403 になる）
- **登録は `register_model_version()`。** `candidate` か `retired` しか作れず、
  **配信中（`active` / `shadow`）の版は上書きできない**（同じ名前で中身が変わると、
  `station_forecasts.model_version` が指す先が変わる）
- `station_forecasts` と `inference_log` の `model_version` に**外部キーは張っていない**
  （保持で消える表から参照すると、古い予測が消せなくなる）

**`feature_set` の照合は種類で違う**（W4-17）。

| 種類 | 読む列 | 版が違ったら |
|---|---|---|
| `lightgbm` | **62 列すべて** | **配らない。** 同じ名前で意味の違う列を見る（例外は出ず確率だけが変わる） |
| `baseline` | 6 列（システム・ポート・日・水平・日内分・目標の曜日種別） | 配る。**v0 から v3 までこの 6 列は 1 つも変わっていない** |

いま配っている `baseline-b3-v0-20260908` は **`feature_set = v0`** で、配信側は v3 である。

**モデルの成果物**：**`models` バケット**。どちらも gzip した JSON で、**`format_version` を先頭に持つ**。

| パス | 中身 | 実測 |
|---|---|---|
| `baseline/{version}.json.gz` | B1 の参照表・B2 の気候値・B3 の係数・ポートの並び | **8.6 MB**（2026-09-13 以降。気候値をプロファイルから作るようになり、使えるセルが 66 万 → 198 万に増えた。W5 の PR D）。それ以前は 3.2 MB |
| `lightgbm/{version}.json.gz` | **木の構造そのもの**（節ごとの特徴量・閾値・左右・既定の向き・欠損の扱い・葉の値、カテゴリのビット集合、木の根）を 2 ターゲットぶん ＋ 列の並び・カテゴリ・語彙 ＋ ハイパーパラメータ | **3.1 MB**（節 75,900 × 2 ターゲット） |

**LightGBM の成果物は `format_version = 2`**（2026-09-10、W4 の PR E′）。v1 は `Booster.model_to_string()` を文字列で持ち、**配信側で `lightgbm` に読ませていた**——それが Vercel の Python ランタイムでは読み込めないと分かったので（`libgomp.so.1` が無い。W4 プラン §12 の 126）、**木の構造を持って numpy で歩く**形に変えた。**v1 の成果物は読まずに例外にする**（黙って読み替えない）。同じ値が出ることは、当てはめた直後に `Booster.predict` と突き合わせて確かめている。

**読むときに列の並びと語彙を照合し、違えば配らない。**

## 5. 公開ビュー（`/v1` が読む唯一の面）

匿名ロールに権限があるのは**この 3 つだけ**。基底テーブルには一切手が届かない（pgTAP `0010_public_views.sql` が固定する。**数だけでなく名前も**——1 枚消して 1 枚足したときに素通りしないため）。**列の一覧も pgTAP が固定している**（`columns_are`）。読む側は `select *` をせず列を並べるので（`apps/web/lib/api/view-query.ts`）、片方だけ変えると実行時まで気づけない。

### 5.1 `v1_stations_current`

**内部の約束をビューの外に出さない**ように変換してある。**この変換はビュー側で行っているので、どの経路から読んでも同じ意味になる。**

| ビューの列 | 元 | 変換 |
|---|---|---|
| `bikes` / `docks` | `station_status_latest` | **`-1` → NULL** |
| `is_installed` / `is_renting` / `is_returning` | `flags` | ビット和を真偽値 3 つに開く。**`flags < 0` なら NULL** |
| `name` / `lat` / `lon` | `station_attributes`（`valid_to is null`） | **`left join`。属性が無ければ NULL で返す**（新しいポートは最大 1 日属性を持たない） |
| `capacity` | 同上 | **固定のラック数を持つシステムだけ返す。`capacity_is_dynamic` の系統は NULL**（0035） |
| `is_present` / `last_changed_at` | `station_status_latest` | そのまま |
| `forecast_horizons_min` / `forecast_p_bike_x1000` / `forecast_p_dock_x1000` / `forecast_confidence` / `forecast_base_observed_at` / `forecast_model_version` / `forecast_generated_at` | `station_forecasts` | **`left join`。予測が無ければ 7 列とも NULL**（0033・0034） |

除外：`geo_suspect` のポート、`is_active = false` のシステム。

**`capacity` は「固定のラック数」を意味する列である**（0035。W4 プラン §12 の 115）。ドコモの公開値は日次同期の瞬間の `bikes + docks` が凍結されたもので（§4.3）、そのまま渡すと「容量 5・借りられる 12」という矛盾が画面に出る（実測で 628 ポート・ドコモの 10.8%）。**ビューが NULL にして外に出さない**。`-1` を NULL に、`flags` を真偽値に開いているのと同じ理由である。**基底テーブル（`station_attributes.capacity`）の値は消していない**ので、学習側はそちらを読む。ポートの大きさは `capacity_est`（前日までの 7 日の `max(bikes + docks)`。§7.4 の参照スナップショット）で推定する。

**予測の鮮度はビューで切らない**（0033）。「何秒より古ければ出さないか」は `packages/shared/src/constants.ts` の `FORECAST_STALE_AFTER_S`（900 秒）が持っており、SQL にも 900 を書くと正が 2 つになる。現在値の `stale` 判定も TypeScript 側（`freshness.ts`）にあって iOS と共有しているので、揃えてある。**ビューは `base_observed_at` をそのまま渡し、切るかどうかは読む側が決める。**

**予測の時刻には 2 つある。取り違えない**（0034。W4 プラン §12 の 114）。

| 列 | 意味 | 使い道 |
|---|---|---|
| `forecast_base_observed_at` | その予測が**どの観測に基づくか**（台数の観測時刻） | **鮮度の判定**。`FORECAST_STALE_AFTER_S`（900 秒）より古ければ出さない |
| `forecast_generated_at` | 推論を**回した時刻** | **水平の起点**。`p_*_x1000[i]` が指すのは「これ ＋ `horizons_min[i]` 分後」 |

`jobs/infer.py` は推論時刻（`now`）で標本を作る（`minute_of_day` は `now`、`dow_type` は `now + horizon`）ので、**水平は `generated_at` から数える**。`base_observed_at` から数えると、その差（推論の所要ぶん）だけずれる。読む側は `in_min ＋（いま − generated_at）` の位置で補間する。

**予測の付くポートの割合（2026-09-09 05:00 UTC 実測）**：HELLO 14,801 / 14,928（**99.15%**）、ドコモ 5,798 / 5,822（**99.59%**）。付かないのは、貸出も返却も止まっている・観測が `-1`・成果物に無いポートで、**行は返り台数は出る**（W4-02）。

**`lat` / `lon` は NULL になりうる**（属性が未着、または失効）。**bbox で引けば範囲比較で自然に外れるが、ID で引くと出てくる**（`/v1/trip-check`）。実測（2026-09-11、本番）で**座標の無いポートは 10 件**あり、**うち 1 件は予測を持っていた**——「座標が無い＝予測も無い」ではない（W4 プラン §12 の 129）。読む側は `hasLocation` で明示的に外す。

### 5.2 `v1_feeds`

`system_id` / `display_name` / `expected_cadence_s` / `poll_interval_s` / `capacity_is_dynamic` / `last_observed_at` / `forecast_model_version` / `forecast_generated_at`。稼働中のシステムのみ。**鮮度はフィード単位の値**なので、ポート単位のビューには入れていない。

**`forecast_model_version` / `forecast_generated_at` は `inference_log` から引く**（0033）。`station_forecasts` から取ると版を知るのに 2 万行を走査することになるが、`inference_log` なら `id` の降順に 1 行見るだけで済む。**`status = 'ok'` の最新行**を使い、失敗した回と実行中の回は数えない。1 度も成功していなければ NULL（W2 からの意味を変えない）。

### 5.3 `v1_station_neighbors`

`system_id` / `station_id` / `nb_system_id` / `nb_station_id` / `distance_m` / `same_system`。**稼働中のシステムどうしの組だけ**（どちら側が停止していても落とす）。`station_neighbors` を素直に写したもので、日次の `rebuild_station_neighbors` が全置換する。

**片方向の行を両向きぶん持つ**ので、`station_id` で絞れば「そのポートの近傍」がそのまま出る。

**絞り込みはビューに焼き付けていない**（0039、W4-26）。`distance_m <= 400` も `same_system` も「今日の `/v1/trip-check` の都合」であって、近傍という事実の性質ではない。**絞るのは問い合わせ側**（`apps/web/lib/api/view-query.ts` の `neighborFilters`）で、`v1_stations_current` が bbox を焼き付けていないのと同じ形である。

**なぜ bbox で代用しなかったか**：座標から距離を出し直すことになり、**距離の実装が SQL（`rebuild_station_neighbors`）と TypeScript の 2 つ**になる。同じ 2 点に別の距離を出す状態を作らない。

---

## 6. RPC 関数（PostgREST から呼ぶ）

`service_role` だけが実行できる。匿名からは呼べない。

| 関数 | 役割 |
|---|---|
| `begin_fetch(system_id, min_interval_s)` | 取得の開始を宣言し、二重起動を抑止。前回の `etag` / `observed_at` を返す |
| `ingest_snapshot(...)` | **スナップショットの取り込み。台帳の採番・配列の組み立て・`station_status_latest` と `feed_state` の更新を 1 トランザクションで行う** |
| `finish_fetch(system_id, log)` | 取得の記録（`feed_fetch_log`） |
| `upsert_station_attributes(system_id, fetched_at, rows)` | 属性の SCD2 更新 |
| `job_started(name)` / `job_finished(id, status, detail)` | ジョブの記録 |
| `weather_grid_cells(lat_step, lon_step)` | ポートが分布する気象格子を返す |
| `snapshot_partition_exists(at)` | 再構築スクリプトの事前確認 |
| `watchdog_collect()` / `monitor_feeds()` / `run_maintenance(keep_days)` / `refresh_station_activity()` / `compute_daily_quality(date)` | pg_cron が呼ぶ運用ジョブ |

---

## 7. アーカイブのパス規約

**バケットは 5 つある。** 作り直せるかどうかが違うので、混ぜない（W3 プラン §12 の 106）。

| バケット | 中身 | Content-Type | 保持 | 作り直せるか |
|---|---|---|---|---|
| `gbfs-raw` | 生 gzip JSON | `application/gzip` | 無期限 | **一次ソース。作り直せない** |
| `weather-raw` | 天気予報の生 JSON | `application/gzip` | 無期限 | 同上（過去の予報は取り直せない） |
| `gbfs-parquet` | 学習用 Parquet ＋ 学習サンプル ＋ 参照スナップショット | `application/vnd.apache.parquet` | 無期限 | 生 JSON からも Postgres からも作り直せる |
| `models` | モデルの成果物 | `application/gzip` | 無期限 | **同じ版は二度と作れない**（当てはめの時点が違う） |
| **`forecast-log`** | **配った予測**（0042） | `application/vnd.apache.parquet` | **12 か月** | **作り直せるが高い**（決定的なモデル ＋ 保存済みの入力。**天気は 30 日で消える**） |

**`forecast-log` を `gbfs-parquet` に相乗りさせていない**のは、中身が同じ Parquet でも**寿命が違う**からである。混ぜると保持期間を別々に決められない（0027 が `models` で採ったのと同じ判断）。

**`allowed_mime_types` は絞ってある。** Content-Type を付けない実装を弾くためで、違う型を上げようとすると **HTTP 415** で止まる。

### 7.1 生 GBFS JSON（`gbfs-raw`、一次ソース）

```
{system_id}/{YYYY}/{MM}/{DD}/{feed}_{epoch_s}.json.gz     ← 日付は UTC
例: hellocycling/2026/09/08/station_status_1788828095.json.gz
```

- `epoch_s` は**フィードの `last_updated`**（取得時刻ではない）。同じ観測は同じパスに写る
- 中身は **ODPT から受信したバイト列そのもの**。パースして再直列化していない
- Content-Type は `application/gzip`
- 実測：**70 MB/日・1,354 オブジェクト/日**

### 7.2 天気予報（`weather-raw`）

```
{YYYY}/{MM}/{DD}/jma_msm_{hour_epoch}_{NN}.json.gz        ← 日付・時刻は UTC
例: 2026/09/08/jma_msm_1788825600_05.json.gz
```

- `hour_epoch` は**取得時刻を「時」に丸めた** epoch 秒。同じ時間内の再実行は同じパスに写る
- `NN` は分割番号（00〜05）。595 格子を 100 地点ずつ 6 分割
- 実測：**182 kB/時・4.4 MB/日**、1 時間あたり 6 ファイル（`forecast_days = 2` のとき）
- **2026-09-09 以降は `forecast_days = 3` に広げた**（W3 プラン §12 の 77）。**約 265 kB/時・6.4 MB/日**の見込み。ファイル数と分割の規約は変えていない

### 7.3 学習用 Parquet（`gbfs-parquet`）

```
{system_id}/date=YYYY-MM-DD/hour=HH/part.parquet          ← 日付・時刻は UTC
例: hellocycling/date=2026-09-07/hour=21/part.parquet
```

**列は 8 つ。順序も契約に含む**（`parquet-tools` で覗いたときに並びで意味が分かるように）。

| # | 列 | 型 | 意味 |
|---|---|---|---|
| 1 | `system_id` | string | |
| 2 | `station_id` | string | **`idx` は書かない**（再構築で変わり得るため） |
| 3 | `observed_at` | timestamp[ms, UTC] | フィードの `last_updated`。**提供側が名乗る観測時刻** |
| 4 | **`fetched_at`** | timestamp[ms, UTC] | **収集器が取り込みを終えた時刻。学習の as-of はこの列で切る**（§10.3） |
| 5–8 | `bikes` / `docks` / `flags` / `reported_age_s` | int16 | **`-1` は「観測されなかった」。0 と区別する** |

- ソート：`station_id, observed_at`。圧縮：zstd。区間：半開 `[hour, hour+1)`
- **列の数は読み方で変わる**（実測 2026-09-08、pyarrow 25.0.1）

  | 読み方 | 列数 | `date` / `hour` |
  |---|---|---|
  | `pq.read_table(1 ファイル)` | 8 | 付かない |
  | `ds.dataset(..., partitioning="hive")` | **10** | 付く（`date` は string、`hour` は **int32** でゼロ埋めが落ちる） |
  | `ds.dataset(..., partitioning="hive", schema=SCHEMA)` | **8** | **付かない**（明示スキーマがパーティション列より優先される） |

  **`date` / `hour` が要るなら `SCHEMA` に足した版を渡す。** どちらにせよ `observed_at` から導けるので、ふつうは要らない
- **配列長より外側の `idx` は行にしない**（未登録と欠損を混ぜない）
- 観測が 0 件の時間帯には**ファイルを置かない**
- 冪等：同じ時間帯を 2 回処理すると同じ行になる（実測ではバイト列まで一致した）
- 実測：**4.05 MB/日・1,047 万行/日**（1 時間あたり約 44 万行。`fetched_at` を足す前）

> **Parquet には `-1` がそのまま入っている**（ビューと違って NULL に開いていない）。学習ローダーが必ず落とすこと（§10.1）。

> **⚠️ 読むときは必ず明示スキーマを渡す。**
>
> ```python
> from bikechance_ml.jobs.snapshot_table import SCHEMA
> ds.dataset(root, format="parquet", partitioning="hive", schema=SCHEMA)
> ```
>
> スキーマを渡さないと、pyarrow は**最初に見つけたファイル**から推論する。列構成の違うファイルが混ざっていると**足りない列を黙って落とし、エラーも出ない**。しかも「最初」はディレクトリの列挙順なので**環境によって結果が変わる**（W2 プラン §14.3 の実測）。
>
> **列数で比べない。** 上表のとおり読み方で 8 にも 10 にもなる。**名前の集合で比べる。**
>
> **⚠️ 明示スキーマは「無い列を作る」ためのものではない。** 足りない列は**黙って null で埋まる**。`fetched_at` の無い古い形のファイルを読むと、as-of が全滅しているのに例外も警告も出ない（W3 プラン §12 の 97）。読む前に**ファイル自身の列**を確かめること。
>
> ```python
> from bikechance_ml.jobs.snapshot_table import read_table
> table = read_table(body, "hellocycling 2026-09-07T00Z")   # 違えば SchemaMismatchError
> ```
>
> **同じパスの中身が変わる**（畳み直し）ので、パスで引くだけのキャッシュも同じ罠を踏む。

> **`fetched_at` は 2026-09-08 に追加し、同じ変更で全期間（2026-09-06 06:00 UTC 以降の 47 時間）を畳み直した**（W3 プラン §5.4）。**列が混ざる期間は作っていない。** `n_stations` と `is_anomalous` は Parquet から導けるので足していない（`bikes >= 0` の行数、およびその割合が 0.5 未満か）。

**ファイルが無い理由は 2 つある。混同しない。**

| 理由 | 見分け方 |
|---|---|
| その時間に観測が無かった | `status_snapshots` にも行が無い |
| **まだ畳んでいない**（ジョブ稼働前・失敗・埋め戻し漏れ） | `status_snapshots` には行が在る |

**「パスが無い＝観測が無い」と読んではいけない。** 実際、2026-09-08 にこの文書を書きながら **15 時間ぶんの畳み忘れ**（毎時ジョブが動き出す前の期間）を見つけて埋め戻している。学習期間を切るときは **§13 の欠落検査を必ず走らせる。**

---

### 7.4 学習サンプル（`gbfs-parquet` の `features/`）

```
features/date=YYYY-MM-DD/part.parquet          ← 日付は **JST**（入力の Parquet は UTC）
例: features/date=2026-09-07/part.parquet
```

**1 日 1 ファイルで、両システムが 1 つに入る。** 近傍がシステムを跨ぐので（§4.10）、位置の割り当てを分けられない。`system_id` は列。

**列の正は `apps/ml/bikechance_ml/features/schema.py` の `SCHEMA`。** **69 列**（特徴量 61・キーとラベルと重み 8）。

**`feature_set` は v3**（2026-09-10〜）：**天気の 4 列が入った**（`precip_mm_now` / `precip_mm_target` / `temp_c` / `wind_kmh`。W4 プラン §6.4）。**天気アーカイブは 2026-09-07 15:17 UTC からなので、それより前のサンプルは 4 列とも NULL になる。** **v2 と混ぜて学習しない。**

- v2（2026-09-10）：列は増えていないが、**`minutes_since_last_change` に上限（180 分）が入った**（W4-10）。この列だけが**読み込んだ窓の長さで値が変わる**列で、25 時間読む学習と 3 時間読む推論で必ず食い違っていた

- v1（2026-09-09）：参照データを日次スナップショットから読むようにしたので `capacity` / `fill_ratio` / `is_over_capacity` / `nb_fill_ratio_mean_500m` の値が変わる（W3 プラン §14.6）
- **推論も同じ 61 列を作る**（W4 の PR C・PR D）。出力は `SERVING_SCHEMA` の 65 列（69 から `y_bike` / `y_dock` / `weight` / `stratum` を抜いたもの）で、**学習の 1 点と完全に一致する**ことをゴールデンで固定している（`tests/test_features_parity.py`）

| 群 | 数 | 中身 |
|---|---|---|
| キー | 4 | `system_id`・`station_id`・`t`（基準時刻、UTC で持つ）・`h_min` |
| ラベル | 2 | `y_bike`・`y_dock` |
| 抽出 | 3 | `weight`・`stratum`・`feature_set` |
| 暦 | 13 | `minute_of_day` とその sin/cos、`dow`、`day_type`、`dow_type`、祝日まわり、`target_*` |
| ポート静的 | 9 | `lat`/`lon`、`pref_code`/`muni_code`、`region_id`、`is_charging_station`、`station_age_days`、`capacity`、`urban_density_500m` |
| 現在状態 | 7 | `bikes`・`docks`・`gap`・`fill_ratio`・`is_over_capacity`・`staleness_s`・`feed_delay_s` |
| ラグとトレンド | 11 | `bikes_lag_{5,10,15,30,60}`・`delta_{15,30,60}`・`roll_{mean,min,max}_60` |
| 流量 | 4 | `rentals_60`・`returns_60`・`n_changes_60`・`minutes_since_last_change` |
| 同時刻履歴 | 3 | `bikes_same_time_1d`・`y_bike_same_time_1d`・`y_dock_same_time_1d` |
| 近傍 | 9 | `nb_count_*`・`nb_bikes_sum_*`・`nb_docks_sum_*`・`nb_n_empty_500m`・`nb_fill_ratio_mean_500m` |
| **天気** | **4** | **`precip_mm_now`**（`t` を含む 1 時間帯の降水量 mm）・**`precip_mm_target`**（`t + h` を含む 1 時間帯。**水平で変わる唯一の天気列**）・**`temp_c`**（`t` に最も近い毎正時の瞬時値）・**`wind_kmh`**（同、**km/h**） |

**as-of の規則**（開発プラン §6.2、W3 プラン §9.3）

| 用途 | 切り方 |
|---|---|
| 特徴量（水準・ラグ・同時刻履歴） | **`fetched_at <= t`** のうち `observed_at` が最大。`t − observed_at > 600 秒`なら欠損 |
| ラベル | **`observed_at <= t + h`** のうち最大。`fetched_at` では切らない |
| 流量 | **フィード本来の周期**で計算し、その結果を `t` に as-of する |

> **`feed_delay_s` は HELLO でほぼ定数（201〜209 秒、中央 206）。** 公開周期 300 秒とグリッド 5 分の位相がほぼ固定なので、どの基準時刻でも「1 つ前の公開」を指す。**水平 5 分は観測から見れば実質 8.4 分先である。** ドコモは 30〜183 秒（中央 69）に散る（W3 プラン §12 の 98）。

**除外**（`t` 時点で判定できるものだけ。§10.1）：`t` または `t+h` の as-of が無い / 600 秒より古い / 値が `-1` / `t` 時点で運用停止 / 実在しないポート（ドコモ `5753`）/ **衝突**（`as-of(t+h)` が `as-of(t)` と同じ観測。実測 0 件）。

**抽出と重み**：行ごとに一様乱数を **1 回**引き、難所（`bikes(t) <= 2` または `docks(t) <= 2`）は 4%、それ以外は 1% で採る。重みは `1/抽出率` なのでちょうど 25 と 100。乱数は `(日付, system_id, station_id)` の blake2b から splitmix64 で導く**決定的な値**で、2 回作れば同じ行が出る。

> **難所は実測で 83.9%**（HELLO 86.9%・ドコモ 76.5%）。**層化はほとんど効いていない**が、重みは厳密なままである（W3 プラン §12 の 99）。閾値の見直しは W4。

**実測（2026-09-07、`feature_set = v0`）**

| | 値 |
|---|---|
| 母集団（ポート × 288 点 × 10 水平） | 59,745,600 |
| 除外を通った | 58,348,946 |
| 出力 | **2,053,383 行 / 34.8 MB**（HELLO 1,505,480・ドコモ 547,903） |
| 重みの総和 | 58,368,375（母集団に対し ＋0.033%） |
| 作るのにかかる時間 | 68.6 秒（最大メモリ 1,065 MB） |

**NULL の多い列**（実測。**意味のある NULL である**）

| 列 | NULL | 理由 |
|---|---|---|
| `region_id` | 73.3% | HELLO には無い |
| `bikes_same_time_1d` | 66.4% | 1 日前の観測がまだ無い（収集開始が 2026-09-06 20:07 JST） |
| `minutes_since_last_change` | 34.6%（**v1 まで**） | v2 からは**上限 180 分に張り付く**ので NULL は「観測が 2 つ未満」だけになる（W4 プラン §4 の W4-10） |
| `pref_code` / `muni_code` / `gap` / `staleness_s` / `is_charging_station` | 26.7% | ドコモには無い |
| `nb_fill_ratio_mean_500m` | 10.3% | 500 m に近傍が無い（§4.10 の 10.2% と一致） |

**作り方**

```
cd apps/ml && ./.venv/bin/python -m bikechance_ml.jobs.build_features \
    --date 2026-09-07 --cache .cache/parquet --out /tmp/features.parquet
```

`--upload` を付けると Storage に置く。**`--cache` のファイルは形を確かめてから使う**（畳み直しで同じパスの中身が変わるため。W3 プラン §12 の 97）。

### 7.5 ポートプロファイル（`gbfs-parquet` の `profiles/`）

```
profiles/date=YYYY-MM-DD/daily.parquet     ← その日ぶんの素の集計
profiles/date=YYYY-MM-DD/profile.parquet   ← 直近 28 日の累計（読むのはこちら）
```

**「このポートの、この曜日種別の、この 15 分枠は、ふだんどうか」を数えた表。** 日付は **JST の暦日**（参照スナップショットと同じ）。

**これは気候値（B2）と同じ量である**（W5-01）。開発プラン §6.3 の `prof_p_bike` と §7.1 の B2 は同じセルを別の名前で呼んでいるので、**表を 1 つにして両方がそこから引く**。

| 列 | 型 | 意味 |
|---|---|---|
| `system_id` / `station_id` | `string` | |
| `dow_type` | `string` | `weekday` / `sat` / `sun_holiday`。**`daily` はその日の 1 種別だけ** |
| `slot15` | `int16` | 15 分枠（0〜95）。**B2 の `slot15` と同じ切り方** |
| `n_days` | `int16` | **`profile` にだけ在る。** セルに寄与した日数 |
| `n` | `int16` | 数えた 5 分格子点。**`daily` は最大 3**（288 ÷ 96） |
| `n_suspended` | `int16` | そのうち貸出も返却も止まっていた点 |
| `n_bike_ok` / `n_dock_ok` | `int16` | `y_bike` / `y_dock` が 1 だった点（`features/labels.py` と同じ式） |
| `sum_bikes` / `sum_bikes_sq` | `int32` | `prof_mean_bikes` / `prof_std_bikes` の材料 |
| `sum_rentals_60` / `sum_returns_60` | `int32` | **60 分の移動窓の合計の和**。`n` で割ると `prof_rentals_per_hour` |

**割らずに和で持つ。** 率にすると 28 日ぶんを足せなくなるし、**分母の取り方を先に決めてしまう**（休止した時間を分母に入れるかは読む側の判断。既定は `n_bike_ok / n`）。

**読むのは基準時刻の前日の版**（`features/profile.source_day`）。開発プラン §6.2 のリーク防止で、参照スナップショットとまったく同じ規則である。

**作り方**：`profile(D) = profile(D-1) + daily(D) - daily(D-28)`。**読むのは 3 ファイルだけ**で、28 日ぶんを毎日読み直さない（`capacity_daily_max` と同じ持ち回り）。

**実測**（2026-09-07〜09-12、本番の毎時 Parquet から）：

| | 値 |
|---|---:|
| 1 日のセル数 | 約 **199 万**（20,800 ポート × 96 枠、被覆 99.7%） |
| `daily.parquet` | **1.5〜2.2 MB/日** |
| `profile.parquet` | 1.5 MB（1 日目）→ **7.85 MB**（6 日・2 曜日種別） |
| 28 日・3 種別での見込み | **12〜15 MB/日** |
| 所要 | 13〜20 秒、ピーク **1.01 GB** |
| **2 日目に下限を満たすセル** | **99.72%**（抽出した `features/` からだと 44.35%） |

### 7.6 配った予測（`forecast-log`）

```
{system_id}/date=YYYY-MM-DD/hour=HH/{base_epoch_s}_{model_version}.parquet   ← 日付・時刻は UTC
例: hellocycling/date=2026-09-12/hour=14/1789224574_baseline-b3-v0-20260908.parquet
```

**1 サイクル 1 ファイル**（5 分毎 × 2 系統 ＝ **576 ファイル/日**）。**`features/` が JST なのに対し、ここは UTC** である——結合する相手が `gbfs-parquet` の実測だから（W4 プラン §6.8 の PR N）。

| 列 | 型 | 意味 |
|---|---|---|
| `station_id` | `string` | **昇順に並べて書く**（並びが圧縮に効く。実測 +24.7%） |
| `base_observed_at` | `string` | **結合の鍵。** ISO 8601・UTC。`station_forecasts` と同じ文字列 |
| `model_version` | `string` | `active` と `shadow` が同じ日に並ぶ |
| `horizons_min` | `list<int16>` | 配列の何番目が何分先か。**ファイル自身が言える** |
| `p_bike_x1000` / `p_dock_x1000` | `list<int16>` | 0〜1000。`station_forecasts` と同じ表現 |
| `confidence` | `int8` | 0〜3。信頼度で切った指標を出すために要る |

**定数は覚え書きではなく列に持つ。** 1 日ぶんを `concat_tables` した瞬間に覚え書きは消えるので、**どの行がどの基準時刻のものかが分からなくなる**。覚え書きに入れるのは**結合に使わないもの**だけ——`format_version` / `system_id` / `generated_at` / `feature_set`。

**実測**（2026-09-12 の 1 サイクル）：

| system | ポート | 1 サイクル | 1 日 | 1 年 |
|---|---:|---:|---:|---:|
| hellocycling | 14,851 | 92,978 B | 26.8 MB | 9.8 GB |
| docomo-cycle | 5,809 | 36,303 B | 10.5 MB | 3.8 GB |
| **合計** | 20,660 | **129,281 B** | **37.2 MB** | **13.6 GB** |

**書けなくても推論は落とさない**（W3-18）。成否は `inference_log.detail.forecast_log`（`"ok"` か `"failed:<例外の種類>"`）に残る。**試し打ち（`?model=`）は書かない**（W4-18）。

---

## 8. 天気予報の読み方

**ここは間違えやすいので独立させる。** 天気は「`t` 時点で入手できた**予報**」を使う決まりで、過去に遡って観測できない唯一の入力である。

### 8.1 ファイルの構造

最上位は**地点の配列**（1 ファイル 100 地点、最後の分割は端数）。

```json
[{
  "latitude": 36.05, "longitude": 136.1875, "elevation": 7.0,
  "timezone": "Asia/Tokyo", "utc_offset_seconds": 32400,
  "hourly": {
    "time": ["2026-09-08T00:00", "2026-09-08T01:00", ...],   ← 長さは可変（§8.2）
    "temperature_2m_jma_msm": [...],       "temperature_2m_best_match": [...],
    "precipitation_jma_msm": [...],        "precipitation_best_match": [...],
    "precipitation_probability_jma_msm": [...],  "precipitation_probability_best_match": [...],
    "wind_speed_10m_jma_msm": [...],       "wind_speed_10m_best_match": [...],
    "weather_code_jma_msm": [...],         "weather_code_best_match": [...]
  },
  "hourly_units": {...}
}]
```

### 8.2 `hourly.time` は **JST のナイーブ文字列**

`utc_offset_seconds` は **32400**（＝ JST）。`"2026-09-08T00:00"` は **JST の 0 時**であって UTC ではない。**UTC と取り違えると 9 時間ずれる。** 他のすべての時刻が UTC なので、ここだけが例外である。

配列は**取得日の JST 0 時から `forecast_days × 24` 時間**を覆う。パスの `hour_epoch` は取得した UTC の時刻なので、両者は一致しない。

例：`jma_msm_1788825600_05.json.gz` は UTC 2026-09-08 00:00（JST 09:00）に取得したファイルで、`time[0] = 2026-09-08T00:00`（JST）、`time[-1] = 2026-09-09T23:00`（JST）。

> **⚠️ `time` の長さを定数として扱わない。** 2026-09-09 に `forecast_days` を **2 → 3** に広げたので、**それより前のファイルは 48 点、以降は 72 点**である。読む側は必ず配列の長さを見る（W3 プラン §12 の 77）。広げたのは、`jma_msm` が値を返すのが**当日 0 時から 76 時間ちょうど**で、3 日 = 72 時間が「欠けない最大」だったため。4 日以上にすると系列の途中から `jma_msm` だけが null になる。

> **⚠️ `hour_epoch` は「入手できた時刻」ではない。** 取得時刻を「時」に丸めた値で、実際に保存されるのは cron の分（`17 * * * *`）＝ **常に `hour_epoch + 18 分**（実測でばらつき 0）。**「`hour_epoch <= t` の最新ファイル」で引くと、`t` が毎正時から 18 分の間にあるとき、まだ発行されていない予報を使うことになる**（5 分グリッド点の 30% が該当）。**`v_weather_files.available_at <= t` で引く**（§8.6）。記録が無い時間帯だけ `hour_epoch + 1 時間` に落とす。

→ **取得時刻が遅いほど、前向きの予報時間は短くなる。** `forecast_days = 2` のとき、JST 23 時に取ったファイルは 24 時間先までしか無かった。**3 に広げた後も性質は同じ**で、JST 23 時のファイルは約 49 時間先まで。予測の最大水平は 180 分なので**最悪でも十分に足りる**が、「常に 72 時間先まである」と思ってはいけない。

### 8.3 モデルが 2 本ある理由と、その違い

`jma_msm`（気象庁 MSM）だけを要求すると **`precipitation_probability` が全件 null** で返る。降水確率は予測の主要な特徴量なので、`models=jma_msm,best_match` の 2 本を 1 要求で取っている。

実測（13 時刻 × 595 格子 × 48 時間 = **371,280 点**。2026-09-08）：

| 系列 | 2 モデルが一致した割合 | null |
|---|---|---|
| `precipitation` | **97.81%** | 0 |
| `weather_code` | **96.10%** | 0 |
| `temperature_2m` | **94.77%** | 0 |
| `wind_speed_10m` | **94.67%** | 0 |
| `precipitation_probability_jma_msm` | — | **100% null**（371,280 件すべて） |
| `precipitation_probability_best_match` | — | 0 |

**食い違いはリード時間ではなく緯度で決まる**（降水量・リード 0〜35 時間）。

| 緯度帯 | 食い違い | 値の数 |
|---|---|---|
| 20〜25 度（先島） | 44.18% | 1,872 |
| 25〜30 度（沖縄・奄美） | 48.02% | 8,892 |
| **30〜35 度** | **0.00%** | 107,640 |
| **35〜40 度** | **0.00%** | 151,632 |
| 40〜45 度（北海道北部） | 20.57% | 8,424 |

**本州・四国・九州（緯度 30〜40 度）では 259,272 値がすべて一致する。** 分かれるのは沖縄・奄美と北海道北部だけで、該当するポートは**全 20,742 の 2% 前後**。おそらく MSM の領域の端で `best_match` が別のモデルを選んでいる。

> **訂正**：W2 プラン §12 の 47 は「日本では決定論的な値は両者一致する」と書いているが、**全域では成り立たない**（東京で測れば一致する）。`best_match` は `jma_msm` の別名ではなく、Open-Meteo が地点ごとに選ぶ別のモデル系列である。**どちらを特徴量に使うかは W4 で決める**（決めるまでは両方を保存し続ける）。本州域では選択が結果を変えない。
>
> 本表の一致率が v1.2（1 ファイル 4,560 点で 90.7〜97.0%）より高いのは、あちらの抽出が緯度帯に偏っていたため。緯度で層別すれば両者は矛盾しない。

### 8.4 格子

`jma_msm` は緯度 0.05°・経度 0.0625° の格子で、要求した座標は格子に丸められて返る（実測：35.681 → 35.7、139.767 → 139.75）。ポートは **595 格子**に分布する。同じ格子のポートは同じ予報になる。

**格子の鍵は整数の添字**（`round(lat / 0.05)`, `round(lon / 0.0625)`）にする。**度のまま突き合わせない**：応答の座標は float32 で、`26.2` が `26.199999` として返る（595 地点のうち 8 件）。

**要求した格子と返ってくる格子が食い違うことがある。** 595 のうち **2 件**で、Open-Meteo が自分の格子に丸め直した結果 1 格子ぶんずれる（毎回同じ）。そこに居る **4 ポート（0.019%）は天気が恒久的に NULL** になる（W4 プラン §12 の 119）。隣の格子で埋めない。

| 要求した格子 | 返ってきた格子 | ポート |
|---|---|---|
| (35.65, 138.75) | (35.65, **138.6875**) | 2 |
| (36.75, 139.5625) | (**36.70**, 139.5625) | 2 |

### 8.5 変数ごとに「その時刻が何を指すか」が違う

| 変数 | 値の意味 | 単位 | 特徴量での引き方 |
|---|---|---|---|
| `precipitation` | **直前 1 時間の合計**（Preceding hour sum） | mm | `t` を**含む**時間帯（`t` 以上で最小の毎正時） |
| `temperature_2m` | 瞬時 | °C | `t` に**最も近い**毎正時 |
| `wind_speed_10m` | 瞬時 | **km/h** | 同上 |
| `weather_code` | 瞬時 | WMO コード | 特徴量には使わない（`weather_hourly` には入っている） |
| `precipitation_probability` | — | — | **`jma_msm` は返さない**（210,630 値すべて null。`best_match` は返す） |

**同じ添字で全部を引くと、気温だけが最大 1 時間先の値になる**（W4 プラン §12 の 121）。

**アーカイブ開始前は埋められない。** 2026-09-07 15:17 UTC（＝ 9/08 00:17 JST）より前の期間は、Historical Forecast API の `previous_dayN` で近似する案があったが、実測で最新の予報と 1 日前の予報は降雨フラグが 15% 食い違う。**ライブのアーカイブだけが厳密に正しい**ので、**その期間は天気列を NULL のままにする**（`FEATURE_SET` が v2 と v3 で分かれる。§7.4）。

---

### 8.6 いつ入手できたか（`v_weather_files`）

```sql
select hour_epoch_s from public.v_weather_files
 where status = 'ok' and available_at <= $1   -- $1 = t
 order by available_at desc limit 1;
```

| 列 | 意味 |
|---|---|
| `hour_epoch_s` | パスに入っている値（取得を始めた時刻を「時」に丸めたもの） |
| `forecast_hour` | 同上を timestamptz にしたもの |
| **`available_at`** | **入手できた時刻**（`job_runs.finished_at`）。**引くのはこの列** |
| `n_saved` / `n_failed` / `n_cells` / `status` | その回の結果 |

> **⚠️ `hour_epoch_s <= t` で引いてはいけない。** 実際の入手は約 18 分後（実測 17.637〜17.658 分）で、`t` が毎正時から 18 分の間にあると**まだ発行されていない予報**を使う（5 分グリッド点の 30%。§8.2）。

完了していない実行（`finished_at is null`）は載らない。**入手できていないものを入手できたことにしない。**

## 9. 値の分布（2026-09-07 の 1 日、全ポート × 全スナップショット）

**モデルの当たりを付ける前に、まずここを見る。**

| 指標 | HELLO | ドコモ |
|---|---|---|
| `bikes = 0` の割合 | **17.41%** | **21.98%** |
| `docks = 0` の割合 | **26.17%** | **25.00%** |
| `bikes = 0` かつ `docks = 0` | 0.138% | **4.047%** |
| `bikes` の平均 / 最大 | 3.56 / 55 | 3.88 / 140 |
| `docks` の平均 / 最大 | 3.87 / **1000** | 4.98 / 93 |
| 休止（`flags = 1`） | 2.553% | 0.410% |
| 未観測（`-1`） | 0.002% | 0.189% |

- **`docks` の最大 1000 は誤りではない。** HELLO には `capacity = 1000` を宣言するポートが **19 件**実在する（横浜の広場など。ほかに 116 が 1 件、100 が 3 件）。事実上「無制限」の意味で、**丸めてはいけない**。表示側で扱いを決める
- **ドコモの `docks = 9997` は実在しないポートである。** `5753`「【監視】メンテナンスポート」1 件だけで、`capacity = 9999`、座標は **lat 34.192 / lon 129.243**（対馬沖の海の上）。事業者が監視用に置いているもので、**学習からも地図からも外す**（§10.1）。`geo_suspect` では捕まらない（日本の外接矩形の内側にあるため）
- **ドコモの「両方 0」が 4.05% と高い**のは、`capacity = bikes + docks` が動的な帰結で、その瞬間に台数も枠も無いポートが常に一定数あるということ。**恒久的な休止ではないので学習から落とさない**（§10.1）

### `gap`（HELLO のみ）＝ `capacity − bikes − docks`

予約の代理指標（開発プラン §3.5）。**HELLO でだけ意味を持つ。**

| | HELLO | ドコモ |
|---|---|---|
| `gap = 0` | **78.88%** | 46.63% |
| `gap > 0` | **21.12%** | 28.64% |
| `gap < 0` | 0.01% | 24.73% |
| 平均 / 最小 / 最大 | 0.337 / −1 / 142 | 0.079 / −54 / 161 |

- HELLO は 8 割が 0、2 割が正。`gap < 0` はほぼ無い（負の `docks` を 0 に丸めた結果、`capacity < bikes` のときだけ −1 になる）
- **ドコモの数字は意味を持たない。** `capacity` が動的値なので `gap` はただの時刻ずれのノイズである（§4.3）。**計算してはいけない**
- **丸めの影響**：HELLO は 1 スナップショットあたり中央値 10 件で `docks` を 0 に丸めている。その行の `gap` は本来より 1 大きい。厳密に扱うなら生 JSON を読む

---

## 10. 学習で使うときの決まり

### 10.1 除外規則（**`t` 時点で判定できるものだけ**を使う）

| 除外する | 条件 |
|---|---|
| フィードの欠損 | `t` または `t+h` の as-of 状態が無い（`t − observed_at > 10 分`） |
| フィード全体の異常 | その時刻の `status_snapshots.is_anomalous`（実測 0 件） |
| ポートが観測されていない | 配列の値が **`-1`**、または **`idx >= array_length`** |
| 休止中のポート | `t` 時点の `flags` に該当ビットが立っていない（bike なら `is_renting`、dock なら `is_returning`）。**実際は `flags = 1` かどうかと同義**（§2 の (9)） |
| **実在しないポート** | ドコモの `5753`（`docks = 9997` / `capacity = 9999` / 座標が海の上）。事業者の監視用（EDA #1） |

**名前で弾いてはいけない。** 実測で「日本バプテスト京都教会」「北山バプテスト教会」が `%テスト%` に、「小林メンテナンス駐車場」が `%メンテナンス%` に一致した。いずれも実在するポートである。**値の妥当性**（`docks >= 2000` など）か、明示した ID の一覧で弾く。

**やってはいけない除外が 2 つある。**

- **`stations.is_active` で除外しない。** 日次ジョブが上書きする**現在の**状態なので、過去のサンプルに遡って効く。9 月に稼働していて 11 月に廃止されたポートは 9 月のサンプルまで消え、**生存者バイアス**が入る
- **`capacity = 0` で除外しない。** ドコモの `capacity` は動的値で、任意の一瞬に 0 のポートが常にある（実測 4.0%）。恒久的な休止ではない。休止の判定は `flags` で行う

**補間はしない。** 欠損は欠損のまま除外する。

### 10.2 as-of 結合

- 基準時刻 `t` は **5 分グリッド（JST 00:00 起点）**
- 状態は **`t` 以前の最新スナップショット**を引く（未来を覗かない）
- `t − observed_at > 10 分` なら欠損扱い。公開遅延の実測（最大 226 秒）に対して十分な余裕がある
- **HELLO は 5 分周期なので、水平 5 分がラベルの分解能の限界**

### 10.3 リークを作らない

| してはいけない | なぜ |
|---|---|
| ランダム split | 時系列。**日単位の時系列分割＋パージ（1 日）** |
| 基準時刻より後の観測を特徴量に使う | 自明なリーク |
| **「現在の」属性を過去のサンプルに使う** | `station_attributes` は `valid_from` / `valid_to` で時点を選べる。**必ず `t` 時点の版を引く** |
| ポート別プロファイルを当日のデータで作る | **前日まで**のデータで計算する |
| 天気の「実況」を使う | 使うのは**その時点で入手できた予報**。実況を使うと本番で再現できない |
| **as-of 結合を `observed_at` で切る** | 本番が使えるのは**取り込み済み**のものだけ。**Parquet の `fetched_at`**（§7.3）で `fetched_at <= t` と切る。HELLO は公開遅延が平均 72 秒・300 秒周期なので、**5 分グリッド点の約 24%** で「学習では見えるが本番ではまだ来ていない」値を掴む |
| **天気ファイルを `hour_epoch <= t` で引く** | 実際の入手は `hour_epoch + 18 分`。**5 分グリッド点の 30%** でまだ発行されていない予報を使う（§8.2） |

### 10.4 学習に使ってはいけない列

| 列 | 理由 |
|---|---|
| `stations.is_active` / `last_seen_at` | 現在の値（§10.1） |
| `station_status_latest.*` | 現在の値しか無い。**学習は `status_snapshots` か Parquet を読む** |
| `station_attributes.capacity`（ドコモ） | 動的値（§4.3） |
| `parking_type` / `parking_hoop` | 分散ゼロ（§3.2） |
| `vehicle_types_available` / `vehicle_docks_available` | `bikes` / `docks` と完全に冗長（§3.1） |
| `feed_fetch_log` / `job_runs` / `alert_state` | 運用ログ。ラベルとは無関係 |

---

## 11. 特徴量の在庫（いま何が作れるか）

**○ = 作れる／△ = 条件つき／✗ = 作れない。** 詳細な設計は開発プラン §6.3。

| 種類 | HELLO | ドコモ | 出どころ | 注意 |
|---|---|---|---|---|
| 現在の台数・返却枠 | ○ | ○ | `bikes` / `docks` | `-1` を落とす |
| 台数の履歴（ラグ・移動平均・傾き） | ○ | ○ | `status_snapshots` / Parquet | HELLO は 5 分粒度、ドコモは 81 秒粒度 |
| 運用停止 | ○ | ○ | `flags = 1` | 1 ビットしか無い（§2 の (9)） |
| 容量 | ○ | **△** | `station_attributes.capacity` | ドコモは `max(bikes+docks)` を推定する |
| `gap`（予約の代理） | ○ | **✗** | `capacity − bikes − docks` | ドコモでは意味を持たない |
| 充電ステーション | ○（1.8%） | ✗ | `raw->>'is_charging_station'` | HELLO 専用 |
| エリア | ✗ | ○（15+ 区分） | `raw->>'region_id'` | ドコモ専用。**名前は未収集**（§12） |
| 座標・地理特徴 | ○ | ○ | `lat` / `lon` | `geo_suspect` の 1 件に注意 |
| 行政区画（都道府県・市区町村） | ○ | **✗** | `stations.pref_code` / `muni_code` | 住所から導く（§4.9）。ドコモは住所が無く NULL |
| 近傍のポート（500 m / 300 m） | ○ | ○ | `station_neighbors` | **近傍 0 は実データ**。合計は 0、平均は NULL（§4.10） |
| 天気（気温・降水・降水確率・風速・天気コード） | ○ | ○ | `weather-raw` | 時刻は JST（§8.2）。モデル 2 本（§8.3） |
| 情報の鮮度 `reported_age_s` | ○ | **✗** | `reported_age_s` | ドコモは恒常的に 0 ＝**情報が無い** |
| 車種別の在庫 | **✗** | ✗ | — | 冗長で意味を持たない（§3.1） |
| 曜日・時刻・祝日 | ○ | ○ | `observed_at` ＋ `jp_holidays` | `day_type` 7 値・`dow_type` 3 値（§4.8） |
| 同時刻履歴（1 日前） | **△** | **△** | `features/` の `bikes_same_time_1d` | 蓄積が短く、いまは 66% が NULL（§7.4） |

---

## 12. まだ無いもの・未確定

| 項目 | 状態 | いつ |
|---|---|---|
| ドコモの `region_id` に対応する地域名 | `system_regions.json` を収集していない | 必要になったら |
| `vehicle_type_id` の意味 | `vehicle_types.json` を収集していない | 車種が増えたら |
| 天気のバックフィル近似 | **未確定**。アーカイブ開始（2026-09-08 00:17 JST）より前をどう埋めるか | W4 |
| どちらの天気モデルを使うか | **未確定**。両方を保存し続けている | W4 |
| イベントのカレンダー（花火・祭・スタジアム） | 無い（祝日は §4.8 で入った） | W5 以降 |
| モデル版（`model_versions`）・日次評価（`model_daily_metrics`） | **テーブルがまだ無い**（`station_forecasts` と `inference_log` は §4.11・§4.12 で作った） | W4 |
| 履歴プロファイル（`prof_*`）・再配置・天気の特徴量 | **無い**（`features/` の v0 には列ごと入れていない） | W4〜W5 |

---

## 13. すぐ使える問い合わせ

```sql
-- ある時刻の全ポートの状態（as-of 結合の最小形）
with snap as (
  select * from public.status_snapshots
   where system_id = 'hellocycling' and observed_at <= timestamptz '2026-09-07 12:00+09'
   order by observed_at desc limit 1
)
select s.station_id,
       (select bikes[s.idx + 1] from snap) as bikes,
       (select docks[s.idx + 1] from snap) as docks,
       (select flags[s.idx + 1] from snap) as flags
  from public.stations s
 where s.system_id = 'hellocycling'
   and s.idx < (select array_length(bikes, 1) from snap)   -- 未登録を落とす
   and (select bikes[s.idx + 1] from snap) >= 0;           -- 未観測を落とす

-- ある時点の属性（SCD2 を時点で引く）
select station_id, name, lat, lon, capacity
  from public.station_attributes
 where system_id = 'docomo-cycle'
   and valid_from <= timestamptz '2026-09-07 12:00+09'
   and (valid_to is null or valid_to > timestamptz '2026-09-07 12:00+09');

-- Parquet の欠落検査（**学習期間を切る前に必ず走らせる**）
-- スナップショットが在るのに Parquet が無い時間帯を出す
with pq as (
  select distinct split_part(name, '/', 1) as system_id,
         (substring(name from 'date=(\d{4}-\d{2}-\d{2})') || ' ' ||
          substring(name from 'hour=(\d{2})') || ':00:00+00')::timestamptz as hour_utc
    from storage.objects where bucket_id = 'gbfs-parquet'
),
snap as (
  select system_id, date_trunc('hour', observed_at) as hour_utc
    from public.status_snapshots group by 1, 2
)
select s.system_id, s.hour_utc
  from snap s left join pq p on p.system_id = s.system_id and p.hour_utc = s.hour_utc
 where p.hour_utc is null
 order by s.hour_utc;
-- 埋めるには：GET /ml/compact?hour=2026-09-07T20:00:00Z（正時・タイムゾーン必須）

-- 学習に使える日（品質でフィルタする）
select quality_date, system_id, n_snapshots, n_expected, max_gap_s
  from public.daily_quality
 where n_snapshots::numeric / nullif(n_expected, 0) >= 0.95
   and coalesce(max_gap_s, 0) <= 600
 order by quality_date;
```

```python
# 学習用 Parquet を読む（未観測を落とすところまで）
import pyarrow.dataset as ds
import pyarrow.compute as pc

dataset = ds.dataset("gbfs-parquet/hellocycling", format="parquet", partitioning="hive")
table = dataset.to_table(filter=(pc.field("bikes") >= 0))   # -1 を落とす
# ※ idx >= array_length の行はそもそも Parquet に入っていない（§7.3）

# 実際の列（Hive の date / hour が増えて 9 列になる）
# ['system_id', 'station_id', 'observed_at', 'bikes', 'docks', 'flags',
#  'reported_age_s', 'date', 'hour']
#
# 期間で切るときは Hive の列で絞ると、要らないファイルを開かずに済む
table = dataset.to_table(
    filter=(pc.field("date") >= "2026-09-07") & (pc.field("bikes") >= 0)
)
```
