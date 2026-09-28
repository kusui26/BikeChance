"""ベースラインの成果物（W3 プラン §5.10、開発プラン §8.2）。

**学習と配信で同じ実装を使うための入れ物。** `baselines/` の当てはめ結果（B1 の参照表・
B2 の気候値・B3 の係数）を 1 つの JSON に固め、Storage に置く。推論はこれを読み、
`conditional.predict` / `climatology.predict` / `blend.predict` を**そのまま**呼ぶ。
配信用に別の実装を書かない（CLAUDE.md §2 の原則 4）。

**ポートとシステムの並びも一緒に固める。** B1 も B2 も番号で引くので、並びが変われば
別のセルを指す。成果物に無いポートは気候値が引けないだけで、B1 には落とせる。

**gzip した JSON で持つ。書式は 2 つあり、違うのは B2 の 2 欄だけである**（D-34）。

- **版 1**（読むだけ）：使えるセルの鍵を JSON の整数の配列、率を 6 桁に丸めて JSON の
  数の配列で持つ
- **版 2**（書いて読む）：使えるセルを 1 セル 1 ビットの列（`np.packbits`、上位ビット
  から）で、率を 6 桁に丸めて ×10^6 した整数の `<u4` の列で持つ。どちらも生のバイト列を
  base64 にした文字列 1 本

**版 2 にしたのは、開くときのメモリの山のため**（W6 プランの W6-01）。版 1 は数百万の
鍵と率を JSON の数で持つので、開くと Python の数が数百万個できる。**鍵を持たずに
ビット列にする**のは、使えるセルが増えるほど鍵の列が長くなるからである——セルが
ほぼ埋まった成果物（6.0 百万セル）では、鍵の差分を `<u4` で持つと開く山が 0.76 GB に
なり、ビット列なら 0.39 GB で済む（W6 プランの所見 200）。

**版 1 と版 2 は同じ倍精度を読み出す（ビット一致）。** 版 1 は `round(x, 6)` を JSON に
書き、読むと同じ倍精度に戻る。版 2 は同じ `round(x, 6)` を ×10^6 した整数 k を書き、
読むときに `k / 10^6` にする。IEEE の割り算は正しく丸めるので、これは `round(x, 6)` と
同じ倍精度になる。**k は `round` を通してから作る。** `np.rint(x × 10^6)` から直接作ると、
半分の点の近くで k が 1 ずれる（例：0.0029915 の倍精度は、`round` では 0.002991、
`np.rint` では 0.002992 になる）。

**版 1 を読む道を消すのは、版 1 の成果物が active・shadow・candidate のどこにも
無くなってから**（契約 30）。版を上げた PR で読み手を版 2 だけにすると、
デプロイした瞬間に配信中の版 1 が読めなくなる（W6 プランの所見 191）。

**下限の形は欄を足して書く**（W6 の PR A、D-37）：`b2_serve_days`（曜日種別ごとの配る側）・
`b2_fit_offset_days`（学習の行の下げ幅）・`b2_max_days`（曜日種別ごとの厚さ）。**書式の版は
上げない**——配信が読むのは `usable` と率だけで、下限は記録である。**`b2_min_days` は整数の
まま残す**（配る種別のうち最も低い下限）。PR B の読み手は知らない欄を読み飛ばすので、PR A の
書き手が書いた成果物もそのまま読める。欄の無い成果物（PR A より前）は、全曜日種別に
`b2_min_days` を当てた形として読む。
"""

import base64
import gzip
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.baselines import blend, climatology, conditional
from bikechance_ml.features.arrays import Bools, Float64, Int64, UInt32
from bikechance_ml.features.calendar import DOW_TYPE_ORDER

#: 書く書式の版。**読み方を変えたら上げる。** 読める版は `_B2_READERS` が持つ。
FORMAT_VERSION: Final[int] = 2

#: 確率を丸める桁。0.000001 は確率 ×1000 の分解能より十分に細かい。
RATE_DIGITS: Final[int] = 6

#: 版 2 の B2 の率の尺度。**`RATE_DIGITS` 桁に丸めた率が、ちょうど整数になる。**
RATE_SCALE: Final[int] = 10**RATE_DIGITS

#: 版 2 の率の列の型。**リトルエンディアンの符号なし 32 ビットと書いて固定する**
#: （機械の既定に任せない）。
RATE_DTYPE: Final = np.dtype(np.uint32).newbyteorder("<")

#: 版 2 の「使えるか」のビット列の並び。**上位ビットから**（`np.packbits` の既定）。
BIT_ORDER: Final = "big"

#: 版 2 の B2 の欄。**持ち方を名前に書く**——手で開いた人が読み方を取り違えないように。
USABLE_BITS_FIELD: Final[str] = "b2_usable_bits"
RATE_MICROS_FIELD: Final[str] = "b2_rate_micro_u32"

#: `model_versions.kind` に入る値。
KIND: Final[str] = "baseline"


class ArtifactFormatError(ValueError):
    """成果物が書式どおりでない。**黙って欠けた表を配らない**——読まずに止める。"""


def artifact_path(model_version: str) -> str:
    """`models` バケット内のパス。**版がそのままファイル名**になる。"""
    return f"{KIND}/{model_version}.json.gz"


@dataclass(frozen=True)
class TargetModel:
    """1 ターゲット（貸出 / 返却）ぶんの当てはめ結果。"""

    b1: conditional.Table
    b2: climatology.Table
    b3: blend.Blend


@dataclass(frozen=True)
class Artifact:
    """配信に要るものを全部入れた成果物。"""

    #: 書式の版。**読んだものは読んだ版、組み立てたものは `FORMAT_VERSION`**
    format_version: int
    model_version: str
    feature_set: str
    created_at: str
    train_days: tuple[str, ...]
    horizons_min: tuple[int, ...]
    systems: tuple[str, ...]
    #: `"{system_id}/{station_id}"` の並び。B2 のセルの番号がこれに対応する
    ports: tuple[str, ...]
    targets: Mapping[str, TargetModel]

    def describe(self) -> str:
        cells = {name: one.b2.cells for name, one in self.targets.items()}
        return f"{self.model_version}（学習 {len(self.train_days)} 日、気候値のセル {cells}）"


def to_bytes(artifact: Artifact) -> bytes:
    """gzip した JSON（版 2）にする。**同じ成果物からは同じバイト列が出る。**

    **書けるのは版 2 だけ。** 版 1 で読んだ成果物を書き直すなら、
    `replace(artifact, format_version=FORMAT_VERSION)` と明示する（黙って版を変えない）。
    """
    if artifact.format_version != FORMAT_VERSION:
        raise ArtifactFormatError(
            f"書けるのは書式 {FORMAT_VERSION} だけです"
            f"（この成果物は書式 {artifact.format_version}）"
        )
    targets = {name: _target_to_json(one) for name, one in artifact.targets.items()}
    document = {**_header(artifact), "targets": targets}
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
    return gzip.compress(encoded, mtime=0)


def from_bytes(body: bytes) -> Artifact:
    """gzip した JSON を読む。**読めるのは版 1 と版 2**（契約 30）。ほかの版は例外にする。"""
    document = json.loads(gzip.decompress(body).decode())
    version = _readable_version(document["format_version"])
    ports = tuple(str(one) for one in document["ports"])
    return Artifact(
        format_version=version,
        model_version=str(document["model_version"]),
        feature_set=str(document["feature_set"]),
        created_at=str(document["created_at"]),
        train_days=tuple(str(one) for one in document["train_days"]),
        horizons_min=tuple(int(one) for one in document["horizons_min"]),
        systems=tuple(str(one) for one in document["systems"]),
        ports=ports,
        targets={
            name: _target_from_json(one, len(ports), version, name)
            for name, one in document["targets"].items()
        },
    )


# ── 書く ──────────────────────────────────────────────────────
def _header(artifact: Artifact) -> dict[str, object]:
    """ターゲットより上の欄。**版 1 と同じ名前・同じ形**（違うのは B2 だけ）。"""
    return {
        "format_version": artifact.format_version,
        "model_version": artifact.model_version,
        "feature_set": artifact.feature_set,
        "created_at": artifact.created_at,
        "train_days": list(artifact.train_days),
        "horizons_min": list(artifact.horizons_min),
        "systems": list(artifact.systems),
        "ports": list(artifact.ports),
    }


def _target_to_json(model: TargetModel) -> dict[str, object]:
    return {
        "b1_rate": _rounded(model.b1.rate),
        "b1_seen": [bool(one) for one in model.b1.seen],
        "b1_fallback": _rounded(model.b1.fallback),
        "b1_n_systems": model.b1.n_systems,
        **_b2_to_buffers(model.b2),
        "b2_min_samples": model.b2.min_samples,
        **_floor_to_json(model.b2),
        # **B3 の係数は丸めない。** 4 つずつしか無く、標準化の分母を丸めると
        # ゼロ除算になり得る（W3 プラン §12 の 104）。丸めるのは数が多い B1・B2 だけ
        "b3_intercept": float(model.b3.intercept),
        "b3_weights": _exact(model.b3.weights),
        "b3_center": _exact(model.b3.center),
        "b3_scale": _exact(model.b3.scale),
    }


def _b2_to_buffers(table: climatology.Table) -> dict[str, str]:
    """B2 を 2 本の生のバイト列にする。**どのセルかはビット列で、率は使えるセルの順に。**

    `rate[usable]` はセルの番号の小さい順に並ぶ。読むときの `rate[usable] = …` も
    同じ順に入れるので、鍵を持たなくても対応が崩れない。
    """
    usable = np.asarray(table.usable, dtype=np.bool_)
    return {
        USABLE_BITS_FIELD: _to_base64(np.packbits(usable, bitorder=BIT_ORDER).tobytes()),
        RATE_MICROS_FIELD: _to_base64(_to_micros(table.rate[usable]).tobytes()),
    }


def _floor_to_json(table: climatology.Table) -> dict[str, object]:
    """下限の形（D-37）。**`b2_min_days` は整数のまま**、形は欄を足して書く。"""
    floor = table.floor
    return {
        "b2_min_days": floor.legacy_days,
        "b2_serve_days": {dow: floor.serve[dow] for dow in DOW_TYPE_ORDER},
        "b2_fit_offset_days": floor.fit_offset,
        "b2_max_days": None if table.max_days is None else dict(table.max_days),
    }


def _to_micros(rates: Float64) -> UInt32:
    """率を 6 桁に丸めて ×10^6 の整数にする。**`round` を先に通す**（版 1 とビット一致）。

    0〜1 の外を先に止めるので、`<u4`（0〜4,294,967,295）に必ず収まる——`astype` は
    収まらない値を**黙って折り返す**（-1 が 4,294,967,295 になる）。
    """
    rounded = np.asarray(_rounded(rates), dtype=np.float64)
    _refuse_out_of_range(rounded, "書こうとした B2")
    return np.rint(rounded * RATE_SCALE).astype(RATE_DTYPE)


def _to_base64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ── 読む ──────────────────────────────────────────────────────
def _readable_version(value: object) -> int:
    """書式の版。**読めない版なら、何も読まずに止める。**"""
    version = int(str(value))
    if version not in _B2_READERS:
        readable = "・".join(str(one) for one in sorted(_B2_READERS))
        raise ArtifactFormatError(f"成果物の書式が {version} で、読めるのは書式 {readable} です")
    return version


def _target_from_json(document: object, n_ports: int, version: int, name: str) -> TargetModel:
    fields = _as_mapping(document)
    return TargetModel(
        b1=_b1_from_json(fields),
        b2=_b2_from_json(fields, n_ports, version, name),
        b3=_b3_from_json(fields),
    )


def _b1_from_json(fields: Mapping[str, object]) -> conditional.Table:
    return conditional.Table(
        n_systems=int(str(fields["b1_n_systems"])),
        rate=np.asarray(_numbers(fields["b1_rate"]), dtype=np.float64),
        # 参照表の分子と分母は配信では使わない（leave-one-out は学習側だけ）
        total=np.zeros(0, dtype=np.float64),
        positive=np.zeros(0, dtype=np.float64),
        fallback=np.asarray(_numbers(fields["b1_fallback"]), dtype=np.float64),
        seen=np.asarray([bool(one) for one in _as_sequence(fields["b1_seen"])], dtype=np.bool_),
    )


def _b2_from_json(
    fields: Mapping[str, object], n_ports: int, version: int, name: str
) -> climatology.Table:
    """B2 を読む。**版で違うのは「どのセルか」と率の持ち方だけ**で、戻す表は同じ形。"""
    rate, usable = _B2_READERS[version](fields, n_ports * _CLIMATOLOGY_CELLS_PER_PORT, name)
    return climatology.Table(
        n_ports=n_ports,
        rate=rate,
        # 分子・分母・件数・日数は配信では読まない（引き算は学習側だけ）
        total=np.zeros(0, dtype=np.float64),
        positive=np.zeros(0, dtype=np.float64),
        counted=np.zeros(0, dtype=np.int64),
        days=np.zeros(0, dtype=np.int64),
        usable=usable,
        min_samples=int(str(fields["b2_min_samples"])),
        floor=_floor_from_json(fields, name),
        max_days=_max_days_from_json(fields, name),
    )


def _floor_from_json(fields: Mapping[str, object], name: str) -> climatology.DayFloor:
    """下限の形を読む。**欄が無ければ PR A より前の成果物**で、全曜日種別が `b2_min_days`。

    欄があるときは `b2_min_days` と食い違わないことも確かめる（配る種別の最も低い下限）。
    """
    legacy = int(str(fields["b2_min_days"]))
    serve = fields.get("b2_serve_days")
    if serve is None:
        return climatology.DayFloor.uniform(legacy)
    floor = _day_floor(serve, fields.get("b2_fit_offset_days", 0), name)
    if floor.legacy_days != legacy:
        raise ArtifactFormatError(
            f"{name}: b2_min_days（{legacy}）が下限の形（{floor.describe()}）と食い違う"
        )
    return floor


def _day_floor(serve: object, offset: object, name: str) -> climatology.DayFloor:
    if not isinstance(serve, dict) or not all(_is_days(one) for one in serve.values()):
        raise ArtifactFormatError(f"{name}.b2_serve_days: 曜日種別ごとの日数（か null）を期待した")
    if not _is_whole(offset):
        raise ArtifactFormatError(f"{name}.b2_fit_offset_days: 整数を期待した")
    try:
        return climatology.DayFloor(serve=serve, fit_offset=int(str(offset)))
    except climatology.FloorError as error:
        raise ArtifactFormatError(f"{name}: 下限の形が組めない（{error}）") from error


def _max_days_from_json(fields: Mapping[str, object], name: str) -> dict[str, int] | None:
    """曜日種別ごとの厚さ。**無い（PR A より前か、読み直した表から書いた）なら None。**"""
    value = fields.get("b2_max_days")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != set(DOW_TYPE_ORDER)
        or not all(_is_whole(one) for one in value.values())
    ):
        raise ArtifactFormatError(f"{name}.b2_max_days: 曜日種別ごとの整数を期待した")
    return {str(dow): int(str(days)) for dow, days in value.items()}


def _is_whole(value: object) -> bool:
    """JSON の整数か（**真偽値は整数に数えない**——`True` は `int` の子である）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_days(value: object) -> bool:
    return value is None or _is_whole(value)


def _b3_from_json(fields: Mapping[str, object]) -> blend.Blend:
    return blend.Blend(
        intercept=float(str(fields["b3_intercept"])),
        weights=np.asarray(_numbers(fields["b3_weights"]), dtype=np.float64),
        center=np.asarray(_numbers(fields["b3_center"]), dtype=np.float64),
        scale=np.asarray(_numbers(fields["b3_scale"]), dtype=np.float64),
    )


def _b2_from_numbers(fields: Mapping[str, object], size: int, name: str) -> tuple[Float64, Bools]:
    """版 1：鍵と率を JSON の数の配列から戻す。"""
    keys = np.asarray(_numbers(fields["b2_keys"]), dtype=np.int64)
    usable = _usable_from_keys(keys, size, name)
    rates = np.asarray(_numbers(fields["b2_rate"]), dtype=np.float64)
    return _fill(usable, rates, name), usable


def _b2_from_buffers(fields: Mapping[str, object], size: int, name: str) -> tuple[Float64, Bools]:
    """版 2：ビット列から「使えるか」を、×10^6 の整数から率を戻す。

    **割り算は倍精度で 1 回だけ**にする。これが `round(x, 6)` と同じ倍精度になる
    （冒頭の注記）。
    """
    usable = _usable_from_bits(fields, size, name)
    raw = _bytes(fields, RATE_MICROS_FIELD, name, RATE_DTYPE.itemsize)
    micros = np.frombuffer(raw, dtype=RATE_DTYPE)
    return _fill(usable, micros / RATE_SCALE, name), usable


def _usable_from_keys(keys: Int64, size: int, name: str) -> Bools:
    """版 1 の鍵を「使えるか」の表にする。**昇順で、表の内側でなければ止める。**

    `usable[keys] = True` に任せると、**鍵が負なら後ろから数えて黙って別のセルが立ち**、
    重なった鍵は 1 つに潰れて率の数と合わなくなる。どこが壊れたかも言わない。
    """
    if len(keys) and not bool(np.all(keys[1:] > keys[:-1])):
        raise ArtifactFormatError(f"{name}: B2 の鍵が昇順でない（重なりか逆戻り）")
    if len(keys) and (int(keys[0]) < 0 or int(keys[-1]) >= size):
        raise ArtifactFormatError(f"{name}: B2 の鍵が表（{size:,} セル）の外を指している")
    usable = np.zeros(size, dtype=np.bool_)
    usable[keys] = True
    return usable


def _usable_from_bits(fields: Mapping[str, object], size: int, name: str) -> Bools:
    """版 2 のビット列を「使えるか」の表にする。**長さがポートの数と合わなければ止める。**

    長さは 8 セルで 1 バイト。1 ポートは 288 セル（36 バイト）なので、**別のポート数で
    書いたビット列**は必ず長さで分かる——黙って読むと、ずれた番号のセルに率が入る。
    """
    raw = _bytes(fields, USABLE_BITS_FIELD, name, _BITS_DTYPE.itemsize)
    packed = np.frombuffer(raw, dtype=_BITS_DTYPE)
    expected = (size + _BITS_PER_BYTE - 1) // _BITS_PER_BYTE
    if len(packed) != expected:
        raise ArtifactFormatError(
            f"{name}.{USABLE_BITS_FIELD}: {len(packed):,} B で、{size:,} セルのビット列"
            f"（{expected:,} B）と長さが合わない"
        )
    return np.unpackbits(packed, count=size, bitorder=BIT_ORDER).view(np.bool_)


def _bytes(fields: Mapping[str, object], field: str, name: str, item_bytes: int) -> bytes:
    """base64 の文字列を生のバイト列に戻す。**壊れていたら止める。**

    `validate=True` を外すと、base64 の外の文字は**黙って捨てられる**。長さは
    1 要素のバイト数（率は 4、ビット列は 1）の倍数でなければならない。
    """
    text = fields.get(field)
    if not isinstance(text, str):
        raise ArtifactFormatError(f"{name}.{field}: base64 の文字列が無い")
    try:
        raw = base64.b64decode(text, validate=True)
    # 字が壊れていれば binascii.Error、ASCII の外なら ValueError（前者は後者の子）
    except ValueError as error:
        raise ArtifactFormatError(f"{name}.{field}: base64 として読めない") from error
    if len(raw) % item_bytes:
        raise ArtifactFormatError(f"{name}.{field}: {len(raw):,} B は {item_bytes} B の倍数でない")
    return raw


def _fill(usable: Bools, rates: Float64, name: str) -> Float64:
    """使えるセルに率を入れた表。**数と範囲が合わなければ止める。**"""
    cells = int(np.count_nonzero(usable))
    if cells != len(rates):
        raise ArtifactFormatError(f"{name}: B2 の使えるセル {cells:,} 個に、率が {len(rates):,} 個")
    _refuse_out_of_range(rates, name)
    rate = np.zeros(len(usable), dtype=np.float64)
    rate[usable] = rates
    return rate


def _refuse_out_of_range(rates: Float64, name: str) -> None:
    """**率は 0〜1。** 外の値や NaN があれば止める（NaN は比べると偽になるので一緒に落ちる）。"""
    if len(rates) and not (float(rates.min()) >= 0.0 and float(rates.max()) <= 1.0):
        raise ArtifactFormatError(f"{name}: B2 の率に 0〜1 の外の値がある")


#: B2 の読み方 1 つ（欄・セルの数・ターゲットの名前 → 率と「使えるか」）。
type _ReadB2 = Callable[[Mapping[str, object], int, str], tuple[Float64, Bools]]

#: 書式の版ごとの B2 の読み方。**ここに在る版だけを読む**（契約 30）。
_B2_READERS: Final[Mapping[int, _ReadB2]] = {1: _b2_from_numbers, 2: _b2_from_buffers}

#: 1 ポートあたりの気候値のセル数（曜日種別 × 15 分枠）。`climatology.cell_key` と同じ形。
_CLIMATOLOGY_CELLS_PER_PORT: Final[int] = len(DOW_TYPE_ORDER) * climatology.SLOTS_PER_DAY

#: 版 2 のビット列の 1 要素（8 セルぶん）。
_BITS_DTYPE: Final = np.dtype(np.uint8)
_BITS_PER_BYTE: Final[int] = 8


# ── 小道具 ────────────────────────────────────────────────────
def _rounded(values: Float64 | Int64 | Bools) -> list[float]:
    """確率を丸める。**数が多い列だけ**（B1 の 120 個、B2 の数百万個）。"""
    return [round(float(one), RATE_DIGITS) for one in values]


def _exact(values: Float64) -> list[float]:
    """丸めずに書く。**係数はここを通す。**"""
    return [float(one) for one in values]


def _numbers(value: object) -> list[float]:
    return [float(str(one)) for one in _as_sequence(value)]


def _as_sequence(value: object) -> Sequence[object]:
    if not isinstance(value, list):
        raise TypeError("配列を期待した")
    return value


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise TypeError("オブジェクトを期待した")
    return value
