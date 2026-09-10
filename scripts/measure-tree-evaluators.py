#!/usr/bin/env python3
"""配信の評価器（numpy と LightGBM の C）を同じ条件で比べる（W4 プラン §12 の 128、W4-20）。

**W6 で「numpy のままか、`libgomp` を同梱して LightGBM に戻すか」を決めるための道具。**
この判断は 3 回続けて誤った数字の上で行われたので（「50 倍」「2.3 倍」「7.8 倍」、いずれも
実測 1.7〜1.8 倍とずれた）、**次に決める人が自分で測り直せる状態**にしておく。

## 測るときの落とし穴（全部踏んだ）

1. **エミュレーションで測らない。** Apple Silicon で `--platform linux/amd64` の
   コンテナを使うと、numpy の SIMD が不利に出て**比が 4 倍ずれる**（7.8 倍 対 1.8 倍）。
   比を測るなら**両方を同じネイティブ環境で**回す。
2. **実時間ではなく CPU 秒で比べる。** Vercel は Active CPU 課金なので、
   **並列化は費用を下げない**——実時間は 2.5 倍速くなるが CPU 秒は 49% 増える。
3. **最適化前の実装を 1 度測った値を使わない。** 機械のぶれが 2 倍あるので、
   複数回の**最小**で比べる。

## 使い方

    cd apps/ml
    ../../scripts/measure-tree-evaluators.py --rows 148610          # 木を作って測る
    ../../scripts/measure-tree-evaluators.py --model /path/to.txt   # 既存の木で測る

`--model` には `Booster.model_to_string()` の出力を渡す。**本番の成果物（書式 2）は
木の構造しか持たないので直接は渡せない**——`jobs/fit_lightgbm.py` の当てはめ中に
書き出すか、同じハイパーパラメータで作り直す。

`libgomp` の同梱が効くかは別に確かめる（本番と同じ土台で）：

    docker run --rm --platform linux/amd64 -v "$PWD/vendor:/vendor" \\
      --entrypoint /bin/sh public.ecr.aws/lambda/python:3.12 -c \\
      'pip install -q lightgbm && python -c "
    import ctypes; ctypes.CDLL(\\"/vendor/libgomp.so.1\\", mode=ctypes.RTLD_GLOBAL)
    import lightgbm; print(lightgbm.__version__)"'
"""

import argparse
import resource
import sys
import time
from collections.abc import Callable
from pathlib import Path

import lightgbm as lgb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "ml"))
from bikechance_ml.models import forest as tree

#: 本番と同じ形（W4-16）。列 62・カテゴリ 6。
N_COLUMNS, N_CATEGORICAL = 62, 6

#: 本番と同じハイパーパラメータ（`jobs/fit_lightgbm.py` の `PARAMS`）。
ROUNDS, LEAVES = 300, 127


def train(rows: int, seed: int) -> lgb.Booster:
    """本番と同じ形の木を作る。**節の数と深さを出力して、実物と比べられるようにする。**"""
    values, labels = _sample(rows, seed)
    return lgb.train(
        {
            "objective": "binary",
            "num_leaves": LEAVES,
            "min_data_in_leaf": 1000,
            "learning_rate": 0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "lambda_l2": 10.0,
            "seed": seed,
            "verbose": -1,
            "force_row_wise": True,
            "deterministic": True,
        },
        lgb.Dataset(
            values,
            label=labels,
            categorical_feature=list(range(N_CATEGORICAL)),
            free_raw_data=False,
        ),
        num_boost_round=ROUNDS,
    )


def _sample(rows: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """欠損もカテゴリも混ぜた行列。**本番の分布を真似ない**（速さは木の形で決まる）。"""
    rng = np.random.default_rng(seed)
    values = rng.normal(size=(rows, N_COLUMNS))
    for index in range(N_CATEGORICAL):
        values[:, index] = rng.integers(0, 12, size=rows)
    values[rng.random(values.shape) < 0.05] = np.nan
    labels = (np.nan_to_num(values[:, 10]) + rng.normal(size=rows) > 0).astype(np.float64)
    return values, labels


def measure(label: str, run: Callable[[], object], repeat: int) -> tuple[float, float]:
    """実時間と **CPU 秒**の最小を取る。**費用に効くのは CPU 秒のほう。**"""
    best_wall, best_cpu = float("inf"), float("inf")
    for _ in range(repeat):
        before = resource.getrusage(resource.RUSAGE_SELF)
        start = time.perf_counter()
        run()
        wall = time.perf_counter() - start
        after = resource.getrusage(resource.RUSAGE_SELF)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        best_wall, best_cpu = min(best_wall, wall), min(best_cpu, cpu)
    print(f"  {label:28} 実時間 {best_wall:6.2f} 秒 / CPU {best_cpu:6.2f} 秒", flush=True)
    return best_wall, best_cpu


def compare(booster: lgb.Booster, rows: int, repeat: int) -> None:
    """同じ木・同じ行で 3 通り測り、**一致も確かめる**。"""
    built = tree.flatten(booster.dump_model())
    values, _ = _sample(rows, seed=7)
    print(f"\n{rows:,} 行 × 1 ターゲット", flush=True)
    _, one = measure(
        "LightGBM C（1 スレッド）", lambda: booster.predict(values, num_threads=1), repeat
    )
    _, many = measure("LightGBM C（既定スレッド）", lambda: booster.predict(values), repeat)
    _, ours = measure("numpy（models/forest.py）", lambda: tree.probability(built, values), repeat)
    theirs = np.asarray(booster.predict(values, num_threads=1), dtype=np.float64)
    gap = float(np.abs(theirs - tree.probability(built, values)).max())
    print(f"  numpy / C(1 スレッド) = {ours / one:.1f} 倍   （CPU 秒で比べる）")
    print(f"  並列化の代償        = CPU 秒が {many / one:.2f} 倍")
    print(f"  最大の差            = {gap:.3e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配信の評価器を比べる")
    parser.add_argument(
        "--rows", type=int, default=148_610, help="測る行数（既定は HELLO の 1 周期）"
    )
    parser.add_argument("--fit-rows", type=int, default=300_000, help="木を作るのに使う行数")
    parser.add_argument("--repeat", type=int, default=4, help="最小を取る回数")
    parser.add_argument("--model", default=None, help="既存の木（model_to_string の出力）")
    options = parser.parse_args(argv)

    if options.model:
        booster = lgb.Booster(model_str=Path(options.model).read_text())
    else:
        print(f"木を作っています（{options.fit_rows:,} 行）…", file=sys.stderr)
        booster = train(options.fit_rows, seed=20260910)
    built = tree.flatten(booster.dump_model())
    print(
        f"木 {len(built)} / 節 {built.n_nodes:,} / 深さ {built.max_depth}"
        f"（2026-09-10 の本番の実物: 300 木・75,900 節・深さ 30）"
    )
    compare(booster, options.rows, options.repeat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
