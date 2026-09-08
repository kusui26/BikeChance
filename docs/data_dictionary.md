# BikeChance データ辞書

- **目的**：**学習・推論のパイプラインを書く人が、この 1 枚だけを読んで入力仕様を決められるようにする。** 開発プラン（なぜ・何を作るか）と週次の実装プラン（どう作ったか）とは役割を分ける。ここに書くのは**データそのものの契約**である。
- **正の所在**：スキーマの正は `supabase/migrations`、パス規約の正は `packages/shared` の `storage-path.ts` / `weather-path.ts`、Parquet の正は `apps/ml/bikechance_ml/jobs/snapshot_table.py` の `SCHEMA`。**食い違ったらコードが正で、この文書を直す。**
- **数値**：断りのないものはすべて 2026-09-08 に本番で実測した値。推測は「見込み」と書く。
- **観測期間はまだ短い**：毎分収集は **2026-09-06 20:07 JST** から、天気は **2026-09-08 00:17 JST** から。**分布や「一度も現れていない」という記述は、この期間だけの話である。** 平日 2 日ぶんしか無く、休日・雨天・イベント日を含んでいない。データが貯まったら測り直し、日付を添えて書き換える。
- **更新規則**：列・パス・センチネルの意味を変えたら、**同じ PR でこの文書も直す**。実測値を足すときは測った日付を添える。
- **変更履歴**：**v1.4（2026-09-08）Parquet に `fetched_at` を足し、全期間を畳み直した（§7.3）。読む側は必ず明示スキーマを渡す規約を書いた。** **v1.3（2026-09-08）`OPEN_METEO_FORECAST_DAYS` を 2 → 3 に広げた（§7.2・§8.1・§8.2）。2 モデルの食い違いが緯度で決まることを 371,280 値で測り直した（§8.3）。** v1.2（2026-09-08）W2 完了時点の点検を反映（`job_runs` の保持規則の記述を訂正、天気ファイルの入手時刻、`fetched_at` の代用）。v1.1（2026-09-08）EDA #1 の結果を反映（実在しないポート `5753`、名前での除外が危険なこと）。v1.0（2026-09-08）初版。W2 の PR F。

---

## 1. どこから読むか

| 用途 | 読む先 | 形 | 保持 |
|---|---|---|---|
| **学習** | Storage `gbfs-parquet` | 長形式 Parquet（1 行 = 1 ポート 1 観測） | 無期限 |
| **推論（オンライン）** | Postgres `status_snapshots` / `station_status_latest` | 配列（1 行 = 1 フィード更新） | **60 日** |
| **配信** | Postgres のビュー `v1_stations_current` / `v1_feeds` | 1 行 = 1 ポート | — |
| **原典・作り直し** | Storage `gbfs-raw` | GBFS の生 JSON（gzip） | 無期限 |
| **天気** | Storage `weather-raw` | Open-Meteo の応答そのまま（gzip） | 無期限 |

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
| `pref_code` / `muni_code` | smallint / int | 座標から導く行政区画コード | **W3 まで NULL** |

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

---

## 5. 公開ビュー（`/v1` が読む唯一の面）

匿名ロールに権限があるのは**この 2 つだけ**。基底テーブルには一切手が届かない（pgTAP `0010_public_views.sql` が 26 項目で固定）。

### 5.1 `v1_stations_current`

**内部の約束をビューの外に出さない**ように変換してある。**この変換はビュー側で行っているので、どの経路から読んでも同じ意味になる。**

| ビューの列 | 元 | 変換 |
|---|---|---|
| `bikes` / `docks` | `station_status_latest` | **`-1` → NULL** |
| `is_installed` / `is_renting` / `is_returning` | `flags` | ビット和を真偽値 3 つに開く。**`flags < 0` なら NULL** |
| `name` / `lat` / `lon` / `capacity` | `station_attributes`（`valid_to is null`） | **`left join`。属性が無ければ NULL で返す**（新しいポートは最大 1 日属性を持たない） |
| `is_present` / `last_changed_at` | `station_status_latest` | そのまま |

除外：`geo_suspect` のポート、`is_active = false` のシステム。

### 5.2 `v1_feeds`

`system_id` / `display_name` / `expected_cadence_s` / `poll_interval_s` / `capacity_is_dynamic` / `last_observed_at`。稼働中のシステムのみ。**鮮度はフィード単位の値**なので、ポート単位のビューには入れていない。

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

> **`fetched_at` は 2026-09-08 に追加し、同じ変更で全期間（2026-09-06 06:00 UTC 以降の 47 時間）を畳み直した**（W3 プラン §5.4）。**列が混ざる期間は作っていない。** `n_stations` と `is_anomalous` は Parquet から導けるので足していない（`bikes >= 0` の行数、およびその割合が 0.5 未満か）。

**ファイルが無い理由は 2 つある。混同しない。**

| 理由 | 見分け方 |
|---|---|
| その時間に観測が無かった | `status_snapshots` にも行が無い |
| **まだ畳んでいない**（ジョブ稼働前・失敗・埋め戻し漏れ） | `status_snapshots` には行が在る |

**「パスが無い＝観測が無い」と読んではいけない。** 実際、2026-09-08 にこの文書を書きながら **15 時間ぶんの畳み忘れ**（毎時ジョブが動き出す前の期間）を見つけて埋め戻している。学習期間を切るときは **§13 の欠落検査を必ず走らせる。**

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

> **⚠️ `hour_epoch` は「入手できた時刻」ではない。** 取得時刻を「時」に丸めた値で、実際に保存されるのは cron の分（`17 * * * *`）＝ **常に `hour_epoch + 18 分**（実測でばらつき 0）。**「`hour_epoch <= t` の最新ファイル」で引くと、`t` が毎正時から 18 分の間にあるとき、まだ発行されていない予報を使うことになる**（5 分グリッド点の 30% が該当）。**`hour_epoch + 1 時間` から使う**か、`storage.objects.created_at` / `job_runs.detail` と突き合わせる。

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

### 8.5 まだ決まっていないこと

**過去分にどの発行時点の予報を使うか（バックフィルの近似）は未確定。** アーカイブが始まる前（2026-09-08 00:17 JST より前）の期間について、Historical Forecast API の `previous_dayN` でどこまで代用するかは **W4 で決める**。実測では最新の予報と 1 日前の予報で降雨フラグが 15% 食い違うため、**ライブのアーカイブだけが厳密に正しい**。

---

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
| 行政区画（都道府県・市区町村） | **✗** | **✗** | `stations.pref_code` / `muni_code` | **W3 まで NULL** |
| 天気（気温・降水・降水確率・風速・天気コード） | ○ | ○ | `weather-raw` | 時刻は JST（§8.2）。モデル 2 本（§8.3） |
| 情報の鮮度 `reported_age_s` | ○ | **✗** | `reported_age_s` | ドコモは恒常的に 0 ＝**情報が無い** |
| 車種別の在庫 | **✗** | ✗ | — | 冗長で意味を持たない（§3.1） |
| 曜日・時刻・祝日 | ○ | ○ | `observed_at` から導出 | 祝日表はまだ無い |

---

## 12. まだ無いもの・未確定

| 項目 | 状態 | いつ |
|---|---|---|
| `stations.pref_code` / `muni_code` | **NULL のまま** | W3 |
| ドコモの `region_id` に対応する地域名 | `system_regions.json` を収集していない | 必要になったら |
| `vehicle_type_id` の意味 | `vehicle_types.json` を収集していない | 車種が増えたら |
| 天気のバックフィル近似 | **未確定**。アーカイブ開始（2026-09-08 00:17 JST）より前をどう埋めるか | W4 |
| どちらの天気モデルを使うか | **未確定**。両方を保存し続けている | W4 |
| 祝日・イベントのカレンダー | 無い | W4 |
| 予測（`station_forecasts`）・モデル版（`model_versions`） | **テーブルがまだ無い** | W4 |
| 特徴量の Parquet（`features/date=…`） | 無い | W5 |

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
