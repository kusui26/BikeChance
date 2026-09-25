"""ベースラインの成果物（`baselines/artifact.py`、W3 プラン §5.10、W6 の PR B）。

**この 1 ファイルの主題は「学習したものと配るものが同じであること」。**
書き出して読み直したモデルが、元のモデルと同じ確率を返さなければ、
配信は学習と別のことをしている。

**PR B で主題が 1 つ増えた：版 1 と版 2 が同じ倍精度を配ること**（D-34、契約 30）。
版 2 を書き始めても、配信中の版 1 は当てはめ直すまで読まれ続ける。**書式を替えたせいで
確率が 1 ビットでも動くなら、それは書式の変更ではなくモデルの変更である。**

版 1 のフィクスチャ（`fixtures/baseline_artifact/format_v1.json`）は、**PR B の前の
書き手**（main の 39ec219）で書いたものの gzip を解き、Prettier で整形して置いてある
（CI の `pnpm format` が JSON を見るため。**変わったのは空白だけ**で、値・型・並びは同じ
ことを確かめてある）。**作り直さない**——版 1 の書き手はもう無く、新しい書き手で作ると
版 1 を読む検査にならない。
"""

import base64
import gzip
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

import numpy as np
import pytest

from bikechance_ml.baselines import climatology, conditional
from bikechance_ml.baselines.artifact import (
    FORMAT_VERSION,
    RATE_DIGITS,
    RATE_DTYPE,
    RATE_MICROS_FIELD,
    RATE_SCALE,
    USABLE_BITS_FIELD,
    Artifact,
    ArtifactFormatError,
    from_bytes,
    to_bytes,
)
from bikechance_ml.baselines.climatology import FromSamples
from bikechance_ml.eval.dataset import TARGETS, to_samples
from bikechance_ml.features.arrays import Float64
from bikechance_ml.jobs.fit_baseline import build_artifact, model_version_for
from bikechance_ml.models.predictor import BaselinePredictor
from tests import eval_fixture as fixture
from tests.test_infer import AT, ready_port
from tests.test_infer import features as cycle_table

BIKE, DOCK = TARGETS
DAY0, DAY1, DAY2 = fixture.DAYS

#: 版 1 の成果物（gzip を解き、空白だけ整えた JSON）。**PR B の前の書き手の出力。**
FORMAT_1: Final[Path] = Path(__file__).parent / "fixtures" / "baseline_artifact" / "format_v1.json"


def scenario() -> list[dict[str, object]]:
    """3 日 × 2 システム × 2 水平 × 台数違い。**同じセルが 2 日にまたがる。**"""
    return [
        fixture.row(day, system, station, horizon, bikes, 9 - bikes, 1 if bikes else 0, 1)
        for day in fixture.DAYS
        for system in ("hellocycling", "docomo-cycle")
        for horizon in (5, 60)
        for station, bikes in (("a", 0), ("b", 1), ("c", 7))
    ]


def built(min_days: int = climatology.MIN_CELL_DAYS) -> Artifact:
    samples = to_samples(fixture.to_table(scenario()))
    # **下限は 2 に下げる**（既定の 30 行はフィクスチャでは立たない。§12 の 167）
    return build_artifact(samples, fixture.DAYS, FromSamples(min_samples=2, min_days=min_days))


ARTIFACT = built()


def format_1_body() -> bytes:
    """版 1 の成果物のバイト列。**中身は旧 `to_bytes` の JSON と同じ**（空白だけ違う）。"""
    return gzip.compress(FORMAT_1.read_bytes(), mtime=0)


def same_bits(before: Float64, after: Float64) -> bool:
    """**倍精度のビットまで同じか。** `==` は 0.0 と -0.0 を同じと見なし、NaN を違うと見なす。"""
    return before.shape == after.shape and bool(
        np.array_equal(before.view(np.uint64), after.view(np.uint64))
    )


def rewritten(body: bytes, change: Callable[[dict[str, object]], None], target: str) -> bytes:
    """成果物の 1 ターゲットの欄を書き換えて、包み直す（壊れた成果物を作る）。"""
    document = json.loads(gzip.decompress(body))
    change(document["targets"][target])
    return gzip.compress(json.dumps(document, ensure_ascii=False).encode(), mtime=0)


def raw_field(body: bytes, target: str, field: str) -> bytes:
    """版 2 の欄を base64 から戻したバイト列。"""
    text = json.loads(gzip.decompress(body))["targets"][target][field]
    assert isinstance(text, str)
    return base64.b64decode(text)


def set_field(field: str, value: object) -> Callable[[dict[str, object]], None]:
    def change(fields: dict[str, object]) -> None:
        fields[field] = value

    return change


def set_raw(field: str, raw: bytes) -> Callable[[dict[str, object]], None]:
    return set_field(field, base64.b64encode(raw).decode("ascii"))


def with_b2_rates(rates: Sequence[float]) -> Artifact:
    """貸出の B2 の先頭のセルに、決めた率を入れた成果物（ほかのセルは使わない）。"""
    model = ARTIFACT.targets[BIKE.name]
    usable = np.zeros(model.b2.usable.size, dtype=np.bool_)
    usable[: len(rates)] = True
    rate = np.zeros(model.b2.usable.size, dtype=np.float64)
    rate[: len(rates)] = rates
    b2 = replace(model.b2, rate=rate, usable=usable)
    return replace(ARTIFACT, targets={**ARTIFACT.targets, BIKE.name: replace(model, b2=b2)})


def test_version_names_the_last_training_day() -> None:
    """**いつまでのデータで作ったかが名前で分かる。**"""
    assert model_version_for(fixture.DAYS) == "baseline-b3-v0-20260909"


# ── 書いて読み直す（版 2）────────────────────────────────────
def test_round_trip_keeps_the_shape() -> None:
    restored = from_bytes(to_bytes(ARTIFACT))
    assert restored.format_version == FORMAT_VERSION == 2
    assert restored.model_version == ARTIFACT.model_version
    assert restored.systems == ARTIFACT.systems
    assert restored.ports == ARTIFACT.ports
    assert restored.horizons_min == ARTIFACT.horizons_min
    assert restored.train_days == ARTIFACT.train_days


def test_round_trip_keeps_the_probabilities() -> None:
    """**書き出して読み直しても同じ確率を返す。** ここがずれたら配信は別物になる。"""
    samples = to_samples(fixture.to_table(scenario()))
    restored = from_bytes(to_bytes(ARTIFACT))
    for target in TARGETS:
        before, _ = conditional.predict(ARTIFACT.targets[target.name].b1, samples, target)
        after, _ = conditional.predict(restored.targets[target.name].b1, samples, target)
        assert after.tolist() == pytest.approx(before.tolist(), abs=1e-6)


def test_round_trip_keeps_the_climatology_cells() -> None:
    samples = to_samples(fixture.to_table(scenario()))
    restored = from_bytes(to_bytes(ARTIFACT))
    fallback = np.full(len(samples), 0.5)
    for target in TARGETS:
        before = climatology.predict(ARTIFACT.targets[target.name].b2, samples, fallback)
        after = climatology.predict(restored.targets[target.name].b2, samples, fallback)
        assert after.fell_back == before.fell_back
        assert after.probability.tolist() == pytest.approx(before.probability.tolist(), abs=1e-6)


def test_round_trip_keeps_the_blend_coefficients() -> None:
    restored = from_bytes(to_bytes(ARTIFACT))
    for target in TARGETS:
        before = ARTIFACT.targets[target.name].b3
        after = restored.targets[target.name].b3
        assert after.intercept == pytest.approx(before.intercept, abs=1e-6)
        assert after.weights.tolist() == pytest.approx(before.weights.tolist(), abs=1e-6)
        assert after.scale.tolist() == pytest.approx(before.scale.tolist(), abs=1e-6)


def test_version_2_reads_back_exactly_what_format_1_would_have() -> None:
    """**PR B の検査 ①**：版 2 にして読んだ配列が、版 1 と同じ規則の値にビットまで一致する。

    版 1 が配っていた値は「率は `round(x, 6)`、係数は丸めない」だった。版 2 も同じ値を
    返さなければ、書式を替えただけで確率が動く。
    """
    restored = from_bytes(to_bytes(ARTIFACT))
    for name, model in ARTIFACT.targets.items():
        got = restored.targets[name]
        assert np.array_equal(got.b2.usable, model.b2.usable)
        expected = np.zeros(model.b2.usable.size, dtype=np.float64)
        expected[model.b2.usable] = [
            round(float(one), RATE_DIGITS) for one in model.b2.rate[model.b2.usable]
        ]
        assert same_bits(got.b2.rate, expected)
        assert got.b1.rate.tolist() == [round(float(one), RATE_DIGITS) for one in model.b1.rate]
        assert np.array_equal(got.b1.seen, model.b1.seen)
        assert same_bits(got.b3.weights, model.b3.weights)
        assert same_bits(got.b3.scale, model.b3.scale)
        assert got.b3.intercept == model.b3.intercept


@pytest.mark.parametrize("value", [0.0029915, 0.0069795, 0.0099705, 0.0109675])
def test_a_rate_near_a_half_is_rounded_like_format_1(value: float) -> None:
    """**×10^6 の整数は `round` を通してから作る**（冒頭の注記）。

    ここに並べたのは、**`np.rint(x × 10^6)` と `round(x, 6)` が 1 違う値**である
    （倍精度の掛け算がちょうど半分に丸まる）。`np.rint` で作ると版 1 と違う率を配る。
    """
    naive = float(np.rint(value * RATE_SCALE)) / RATE_SCALE
    assert naive != round(value, RATE_DIGITS), "np.rint と round が食い違う値を選んでいない"
    restored = from_bytes(to_bytes(with_b2_rates([value])))
    assert float(restored.targets[BIKE.name].b2.rate[0]) == round(value, RATE_DIGITS)


def test_any_rate_comes_back_as_its_six_digit_rounding() -> None:
    """**どの率でも `round(x, 6)` と同じ倍精度に戻る**（端の 0 と 1、1e-6 の前後を含む）。

    表の全セルを使えるセルにして埋める（6 ポート × 288 セル）。**ほぼ埋まった成果物**が
    これからの当てはめ直しの普通の形である。
    """
    rng = np.random.default_rng(20260925)
    edges = [0.0, 1.0, 1e-6, 4.999999e-7, 5.000001e-7, 0.5, 0.9999995, 0.1234565]
    cells = ARTIFACT.targets[BIKE.name].b2.usable.size
    rates = [*edges, *rng.random(cells - len(edges)).tolist()]
    restored = from_bytes(to_bytes(with_b2_rates(rates))).targets[BIKE.name].b2
    expected = np.asarray([round(one, RATE_DIGITS) for one in rates], dtype=np.float64)
    assert same_bits(restored.rate[: len(rates)], expected)
    assert int(restored.usable.sum()) == len(rates)


def test_serialisation_is_deterministic() -> None:
    """**同じ成果物からは同じバイト列が出る**（gzip の時刻も固定する）。"""
    assert to_bytes(ARTIFACT) == to_bytes(ARTIFACT)


def test_only_usable_climatology_cells_carry_a_rate() -> None:
    """**全セルぶんの率を書き出さない。** 「使えるか」は 1 セル 1 ビット、率は使えるセルだけ。"""
    body = to_bytes(ARTIFACT)
    model = ARTIFACT.targets[BIKE.name]
    bits = np.unpackbits(np.frombuffer(raw_field(body, BIKE.name, USABLE_BITS_FIELD), np.uint8))
    micros = np.frombuffer(raw_field(body, BIKE.name, RATE_MICROS_FIELD), dtype=RATE_DTYPE)
    assert len(bits) == model.b2.usable.size
    assert int(bits.sum()) == model.b2.cells == len(micros)
    assert model.b2.cells < model.b2.usable.size


def test_version_2_does_not_write_the_format_1_fields() -> None:
    """**版 2 に鍵の配列は無い。** 両方あると、どちらが正か読む人が迷う。"""
    document = json.loads(gzip.decompress(to_bytes(ARTIFACT)))
    for fields in document["targets"].values():
        assert "b2_keys" not in fields
        assert "b2_rate" not in fields
        assert {USABLE_BITS_FIELD, RATE_MICROS_FIELD} <= fields.keys()


def test_an_artifact_without_climatology_cells_round_trips() -> None:
    """**使えるセルが 0 個でも書けて読める**（率の列は空の文字列になる）。

    収集を始めたばかりの日や、下限を上げた日にはこうなる。気候値は全部 B1 に落ちる。
    """
    empty = built(min_days=99)
    assert all(one.b2.cells == 0 for one in empty.targets.values())
    body = to_bytes(empty)
    assert raw_field(body, BIKE.name, RATE_MICROS_FIELD) == b""
    restored = from_bytes(body)
    assert all(one.b2.cells == 0 for one in restored.targets.values())
    assert not restored.targets[BIKE.name].b2.rate.any()


def test_only_format_2_is_written() -> None:
    """**書くのは版 2 だけ。** 版 1 で読んだものを書き直すなら、版を明示して替える。"""
    with pytest.raises(ArtifactFormatError, match="書けるのは書式 2"):
        to_bytes(replace(ARTIFACT, format_version=1))


@pytest.mark.parametrize("value", [float("nan"), -0.25, 1.5])
def test_a_rate_outside_zero_and_one_is_not_written(value: float) -> None:
    """**`<u4` は負の数と大きすぎる数を黙って折り返す**ので、書く前に止める。"""
    with pytest.raises(ArtifactFormatError, match="0〜1"):
        to_bytes(with_b2_rates([0.5, value]))


@pytest.mark.parametrize("version", [0, FORMAT_VERSION + 1])
def test_unknown_format_version_is_refused(version: int) -> None:
    """**読み方を変えたら版を上げる。** 知らない版を、知っている読み方で読まない。"""
    document = json.loads(gzip.decompress(to_bytes(ARTIFACT)).decode())
    document["format_version"] = version
    body = gzip.compress(json.dumps(document).encode(), mtime=0)
    with pytest.raises(ArtifactFormatError, match="書式"):
        from_bytes(body)


def test_climatology_needs_enough_days() -> None:
    """フィクスチャは 3 日あるので、下限（3 日。D-30）でもセルが立つ（W3 プラン §12 の 101）。

    **下限は成果物に書かれる**——どの下限で作った B2 かが、配ったあとも読める。
    """
    assert ARTIFACT.targets["bike"].b2.min_days == climatology.MIN_CELL_DAYS
    assert ARTIFACT.targets["bike"].b2.cells > 0


def test_describe_mentions_the_version_and_cells() -> None:
    text = ARTIFACT.describe()
    assert ARTIFACT.model_version in text
    assert "気候値" in text


# ── 版 1 を読む（契約 30）────────────────────────────────────
def test_the_format_1_fixture_has_the_old_layout() -> None:
    """**このフィクスチャを新しい書き手で作り直していない**ことの確かめ。"""
    document = json.loads(FORMAT_1.read_bytes())
    assert document["format_version"] == 1
    for fields in document["targets"].values():
        assert {"b2_keys", "b2_rate"} <= fields.keys()
        assert USABLE_BITS_FIELD not in fields
        assert RATE_MICROS_FIELD not in fields


def test_a_format_1_artifact_is_still_read() -> None:
    """**PR B の検査 ②**：版 1 の成果物を、新しい読み手が**書いてあるとおりに**読む。

    突き合わせる相手は JSON をそのまま開いた値である（旧い読み手は `float(str(x))` で
    同じ値にしていた）。**B2 の鍵と率、B1、B3 のすべて**を見る。
    """
    document = json.loads(FORMAT_1.read_bytes())
    restored = from_bytes(format_1_body())
    assert restored.format_version == 1
    assert restored.model_version == document["model_version"]
    assert restored.ports == tuple(document["ports"])
    assert restored.train_days == tuple(document["train_days"])
    for name, fields in document["targets"].items():
        model = restored.targets[name]
        assert np.flatnonzero(model.b2.usable).tolist() == fields["b2_keys"]
        assert model.b2.rate[model.b2.usable].tolist() == fields["b2_rate"]
        assert not model.b2.rate[~model.b2.usable].any()
        assert (model.b2.min_samples, model.b2.min_days) == (
            fields["b2_min_samples"],
            fields["b2_min_days"],
        )
        assert model.b1.rate.tolist() == fields["b1_rate"]
        assert model.b1.fallback.tolist() == fields["b1_fallback"]
        assert model.b1.seen.tolist() == fields["b1_seen"]
        assert model.b3.intercept == fields["b3_intercept"]
        assert model.b3.weights.tolist() == fields["b3_weights"]
        assert model.b3.center.tolist() == fields["b3_center"]
        assert model.b3.scale.tolist() == fields["b3_scale"]


def test_the_formats_differ_only_in_the_two_b2_fields() -> None:
    """**版 1 と版 2 は B2 の 2 欄と版の番号だけが違う**（ほかは同じ名前・同じ値）。

    版 1 で読んだものを版 2 に書き直して、残りの欄を JSON のまま比べる。
    """
    before = json.loads(FORMAT_1.read_bytes())
    moved = replace(from_bytes(format_1_body()), format_version=FORMAT_VERSION)
    after = json.loads(gzip.decompress(to_bytes(moved)))
    assert (before.pop("format_version"), after.pop("format_version")) == (1, 2)
    old_targets, new_targets = before.pop("targets"), after.pop("targets")
    assert before == after
    for name, fields in old_targets.items():
        kept = {k: v for k, v in fields.items() if k not in ("b2_keys", "b2_rate")}
        added = {
            k: v
            for k, v in new_targets[name].items()
            if k not in (USABLE_BITS_FIELD, RATE_MICROS_FIELD)
        }
        assert kept == added


def test_format_1_and_2_serve_the_same_probabilities_bit_for_bit() -> None:
    """**PR B の検査 ③**：版 1 と、それを版 2 に書き直したものが、**推論 1 周期ぶんの表**で
    同じ倍精度を返す（全ポート × 全水平。表は本番と同じ経路で作る）。

    **気候値が効いた行があることも確かめる**——全部 B1 に落ちていたら、B2 の書式の
    違いを何も見ていないことになる。本物の成果物（版 1 の 3 つ）では、PR B の検証で
    510 万個の確率がビットまで一致した。
    """
    old = from_bytes(format_1_body())
    new = from_bytes(to_bytes(replace(old, format_version=FORMAT_VERSION)))
    assert (old.format_version, new.format_version) == (1, 2)
    for system in old.systems:
        table = cycle_table(ready_port(), system)
        before = BaselinePredictor(old).predict(system, AT, table)
        after = BaselinePredictor(new).predict(system, AT, table)
        for target in TARGETS:
            assert same_bits(before.probability[target.name], after.probability[target.name])
            assert np.array_equal(before.informed[target.name], after.informed[target.name])
            assert before.informed[target.name].any(), f"{system}: 気候値が 1 行も効いていない"


# ── 壊れた成果物を読まない（PR B の検査 ④）───────────────────
V2: Final[bytes] = to_bytes(ARTIFACT)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        (RATE_MICROS_FIELD, "AAAA!AAA"),  # base64 の外の文字（validate が無いと黙って捨てる）
        (RATE_MICROS_FIELD, "AAAA AAA="),  # 空白も外の文字
        (RATE_MICROS_FIELD, "あいうえ"),  # ASCII の外（binascii.Error ではなく ValueError）
        (RATE_MICROS_FIELD, "AAA"),  # 詰め物が足りない
        (USABLE_BITS_FIELD, "AA=A"),  # 詰め物の位置が違う
    ],
)
def test_broken_base64_is_refused(field: str, text: str) -> None:
    with pytest.raises(ArtifactFormatError, match="base64 として読めない"):
        from_bytes(rewritten(V2, set_field(field, text), BIKE.name))


@pytest.mark.parametrize("value", [None, [1, 2, 3], 42])
def test_a_field_that_is_not_base64_text_is_refused(value: object) -> None:
    for field in (USABLE_BITS_FIELD, RATE_MICROS_FIELD):
        with pytest.raises(ArtifactFormatError, match="base64 の文字列が無い"):
            from_bytes(rewritten(V2, set_field(field, value), DOCK.name))


def test_a_missing_field_is_refused() -> None:
    def drop(fields: dict[str, object]) -> None:
        del fields[RATE_MICROS_FIELD]

    with pytest.raises(ArtifactFormatError, match="base64 の文字列が無い"):
        from_bytes(rewritten(V2, drop, BIKE.name))


def test_a_rate_buffer_that_is_not_whole_numbers_is_refused() -> None:
    """**長さの合わないバッファ**：率は 4 バイトずつ。2 バイト余れば止める。"""
    raw = raw_field(V2, BIKE.name, RATE_MICROS_FIELD) + b"\x00\x00"
    with pytest.raises(ArtifactFormatError, match="4 B の倍数でない"):
        from_bytes(rewritten(V2, set_raw(RATE_MICROS_FIELD, raw), BIKE.name))


@pytest.mark.parametrize("change", ["drop", "add"])
def test_rates_that_do_not_match_the_cells_are_refused(change: str) -> None:
    """**使えるセルと率の数が合わなければ止める**（1 つ足りなくても、1 つ多くても）。"""
    raw = raw_field(V2, BIKE.name, RATE_MICROS_FIELD)
    changed = raw[: -RATE_DTYPE.itemsize] if change == "drop" else raw + raw[: RATE_DTYPE.itemsize]
    with pytest.raises(ArtifactFormatError, match="率が"):
        from_bytes(rewritten(V2, set_raw(RATE_MICROS_FIELD, changed), BIKE.name))


def test_a_cleared_bit_is_refused() -> None:
    """**ビットを 1 つ落とすと、率が 1 つ余る。** 黙って読むと、率が 1 セルずつずれて入る。"""
    bits = bytearray(raw_field(V2, BIKE.name, USABLE_BITS_FIELD))
    first = next(index for index, one in enumerate(bits) if one)
    bits[first] &= bits[first] - 1  # いちばん下の立っているビットを落とす
    with pytest.raises(ArtifactFormatError, match="率が"):
        from_bytes(rewritten(V2, set_raw(USABLE_BITS_FIELD, bytes(bits)), BIKE.name))


@pytest.mark.parametrize("shift_bytes", [-36, -1, 1, 36])
def test_bits_for_another_number_of_ports_are_refused(shift_bytes: int) -> None:
    """**長さの合わないビット列**：1 ポート 288 セルは 36 バイト。別のポート数の列を止める。"""
    bits = raw_field(V2, BIKE.name, USABLE_BITS_FIELD)
    changed = bits[:shift_bytes] if shift_bytes < 0 else bits + bytes(shift_bytes)
    with pytest.raises(ArtifactFormatError, match="長さが合わない"):
        from_bytes(rewritten(V2, set_raw(USABLE_BITS_FIELD, changed), BIKE.name))


def test_a_port_list_that_does_not_match_the_bits_is_refused() -> None:
    """**ポートの並びとビット列が別物**なら止める（ポートを 1 つ落とした成果物）。"""
    document = json.loads(gzip.decompress(V2))
    document["ports"] = document["ports"][:-1]
    with pytest.raises(ArtifactFormatError, match="長さが合わない"):
        from_bytes(gzip.compress(json.dumps(document).encode(), mtime=0))


def test_a_rate_above_one_is_refused() -> None:
    """**率は 0〜1**（×10^6 で 1,000,000 まで）。`<u4` は 42 億まで入るので、中身で止める。"""
    micros = np.frombuffer(raw_field(V2, BIKE.name, RATE_MICROS_FIELD), dtype=RATE_DTYPE).copy()
    micros[0] = RATE_SCALE + 1
    with pytest.raises(ArtifactFormatError, match="0〜1"):
        from_bytes(rewritten(V2, set_raw(RATE_MICROS_FIELD, micros.tobytes()), BIKE.name))


def _keys(change: Callable[[list[int]], list[int]]) -> Callable[[dict[str, object]], None]:
    def apply(fields: dict[str, object]) -> None:
        keys = fields["b2_keys"]
        assert isinstance(keys, list)
        fields["b2_keys"] = change([int(one) for one in keys])

    return apply


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda keys: [keys[1], keys[0], *keys[2:]], "昇順でない"),  # 入れ替わった
        (lambda keys: [keys[0], keys[0], *keys[2:]], "昇順でない"),  # 重なった
        (lambda keys: [-1, *keys[1:]], "表"),  # 負（黙って後ろから数えるところだった）
        (lambda keys: [*keys[:-1], 10**9], "表"),  # 表より先
    ],
)
def test_broken_format_1_keys_are_refused(
    change: Callable[[list[int]], list[int]], message: str
) -> None:
    """**版 1 の鍵も確かめる。** 並びと範囲が崩れた鍵は、`usable[keys] = True` が黙って受ける。"""
    with pytest.raises(ArtifactFormatError, match=message):
        from_bytes(rewritten(format_1_body(), _keys(change), BIKE.name))


def test_format_1_rates_that_do_not_match_the_keys_are_refused() -> None:
    def drop(fields: dict[str, object]) -> None:
        rates = fields["b2_rate"]
        assert isinstance(rates, list)
        fields["b2_rate"] = rates[:-1]

    with pytest.raises(ArtifactFormatError, match="率が"):
        from_bytes(rewritten(format_1_body(), drop, DOCK.name))


def test_a_format_1_rate_above_one_is_refused() -> None:
    def spoil(fields: dict[str, object]) -> None:
        rates = fields["b2_rate"]
        assert isinstance(rates, list)
        fields["b2_rate"] = [1.5, *rates[1:]]

    with pytest.raises(ArtifactFormatError, match="0〜1"):
        from_bytes(rewritten(format_1_body(), spoil, BIKE.name))
