"""特徴量パイプラインの定数（W3 プラン §9.3、開発プラン §6.1・§6.2）。

**マジックナンバーはここに集める**（CLAUDE.md §3）。水平とグリッドは
`packages/shared/src/constants.ts` と同じ値でなければならない。**ずれると学習と
配信が別の世界になる**ので、`tests/test_features_constants.py` が TypeScript の
定義を読んで突き合わせる。
"""

from typing import Final

#: 予測の水平（分）。`packages/shared/src/constants.ts` の `HORIZONS_MIN` と同じ。
HORIZONS_MIN: Final[tuple[int, ...]] = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180)

#: 基準時刻のグリッド（分）。JST 00:00 起点で 1 日 288 点（開発プラン §6.1）。
GRID_MINUTES: Final[int] = 5

#: 1 日の基準時刻の数。
GRID_POINTS_PER_DAY: Final[int] = 24 * 60 // GRID_MINUTES

#: as-of の上限（秒）。これより古い観測は「欠損」として扱う（W3 プラン §9.3）。
#: 公開遅延の実測（最大 226 秒）に対して十分な余裕がある。
MAX_STALENESS_S: Final[int] = 600

#: 特徴量の版。モデルはこの値を記録し、推論時に照合する（CLAUDE.md §2 の原則 4）。
#:
#: v1（2026-09-09）：**参照データを日次スナップショットから読むようにした**（W3 プラン
#: §14.2 の 2）。列は増えていないが、値が変わる 3 つがある。
#:   * `capacity`（動的な容量のシステム）… ビルド窓の累積最大 → **前日までの 7 日の最大**
#:   * `fill_ratio` / `gap` / `is_over_capacity` … 上に乗っているので一緒に動く
#:   * 近傍と静的属性 … その日の値ではなく**前日の版**で固定される（再現性が戻る）
#: v2（2026-09-10）：**`minutes_since_last_change` に上限（180 分）を入れた**
#: （W4 プラン §4 の W4-10）。この列だけが読んだ窓の長さで値が変わり、25 時間読む学習と
#: 3 時間読む推論で必ず食い違っていた。**上限を決めて、どちらも同じ値を出せるようにした。**
#: 値が変わるのはこの 1 列だけだが、**v1 と混ぜて学習しない**。
FEATURE_SET: Final[str] = "v2"

#: 難所の判定（開発プラン §6.2）。`bikes <= 2` または `docks <= 2`。
TIGHT_THRESHOLD: Final[int] = 2

#: 抽出率。**1 回の判定で決める**（W3 プラン §9.3）。一様に引いてから難所を足すと
#: 独立抽選になり、包含確率が 3.97% になって重みが厳密でなくなる。
UNIFORM_RATE: Final[float] = 0.01
TIGHT_RATE: Final[float] = 0.04

#: 層の名前。出力の `stratum` 列に入る。
STRATUM_UNIFORM: Final[str] = "uniform"
STRATUM_TIGHT: Final[str] = "tight"

#: ラグと移動窓（分）。`GRID_MINUTES` の倍数でなければグリッドから引けない。
LAG_MINUTES: Final[tuple[int, ...]] = (5, 10, 15, 30, 60)
DELTA_MINUTES: Final[tuple[int, ...]] = (15, 30, 60)
ROLL_MINUTES: Final[int] = 60

#: 流量の窓（分）。**フィード本来の周期で計算してからグリッドに写す**（W3-11）。
FLOW_MINUTES: Final[int] = 60

#: `minutes_since_last_change` の上限（分）。**これを超えた値は上限に張り付ける。**
#:
#: この特徴量だけは窓の長さで値が変わる（他は 60 分か 1 日で閉じている）。学習は 25 時間
#: さかのぼれるが、推論は 5 分毎に走るので同じ深さは読めない。**上限を決めておけば、
#: どちらも同じ値を出せる**（W4 プラン §4 の W4-10）。
#:
#: 180 分にしたのは、推論が読む窓（3 時間）に収まる最大だから。実測（2026-09-10）では
#: 最後の変化から 60 分以内のポートは HELLO で 36%・ドコモで 56% しかなく、60 分で
#: 切ると**大半が NULL になって情報が消える**。180 分なら「動いていない」ことを
#: 数として残せる。
CHANGE_CAP_MINUTES: Final[int] = 180

#: 近傍の帯（メートル）。`station_neighbors` は 500 m まで持つ（W3 プラン §9.5）。
NEAR_RADIUS_M: Final[int] = 300
FAR_RADIUS_M: Final[int] = 500

#: 読み込む前後の余白（時間）。ラグ 60 分と流量 60 分に加え、同時刻履歴（1 日前）を
#: 賄うために既定で 25 時間さかのぼる。先は最長の水平（180 分）を覆う。
LOOKBACK_HOURS: Final[int] = 25
LOOKAHEAD_HOURS: Final[int] = 3

#: 推論が読む直近の窓（分）。`CHANGE_CAP_MINUTES` を満たせる最小に、as-of の古さの
#: 上限（`MAX_STALENESS_S`）と格子 2 点ぶんの余裕を足したもの。**ここを縮めると
#: `minutes_since_last_change` が学習と食い違う**（W4 プラン §4 の W4-10）。
NOW_LOOKBACK_MINUTES: Final[int] = CHANGE_CAP_MINUTES + MAX_STALENESS_S // 60 + GRID_MINUTES * 2

#: 同時刻履歴の間隔（分）。1 日前。7 日前は蓄積が足りないので v0 では作らない。
SAME_TIME_MINUTES: Final[int] = 24 * 60

#: 「観測されなかった」を表す値。`jobs/snapshot_table.py` の `MISSING` と同じ意味。
MISSING: Final[int] = -1

#: `flags` のビット（データ辞書 §4.4）。実際に現れるのは 7 / 1 / -1 だけ。
FLAG_INSTALLED: Final[int] = 1
FLAG_RENTING: Final[int] = 2
FLAG_RETURNING: Final[int] = 4

#: 実在しないポート（EDA #1。事業者の監視用で `docks = 9997` / 座標が海の上）。
PHANTOM_STATIONS: Final[frozenset[tuple[str, str]]] = frozenset({("docomo-cycle", "5753")})

#: **システムごとの実データの癖**（データ辞書 §11）。列の有無ではなく「意味があるか」で分ける。
#:   * `gap` は HELLO の `capacity − bikes − docks`。ドコモの `capacity` は動的値なので
#:     恒等的に 0 になり、予約の代理にならない
#:   * `reported_age_s` はドコモで恒常的に 0。「常に新鮮」ではなく**情報が無い**
#:   * ドコモの容量は `max(bikes + docks)` から推定する（宣言値は取り込み時点の凍結値）
SYSTEMS_WITH_GAP: Final[frozenset[str]] = frozenset({"hellocycling"})
SYSTEMS_WITH_REPORTED_AGE: Final[frozenset[str]] = frozenset({"hellocycling"})
SYSTEMS_WITH_DYNAMIC_CAPACITY: Final[frozenset[str]] = frozenset({"docomo-cycle"})
