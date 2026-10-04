"""合成器を組んで置く（`jobs/build_composite.py`、W6 の PR H、§8.9）。

**主題は 3 つ。**
  * **部品は置いてあるバイト列のまま**入り、名前は B3 の最終学習日で決まる
  * **置けない名前では測る前に止まり**（契約 38）、置く → 登録の順で、登録は candidate だけ
  * **境目の差**（§5.10、完了条件 3）を測ってカードと `metrics` に出す
"""

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import pyarrow as pa
import pytest

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.features.grid import features_path
from bikechance_ml.io.supabase import PARQUET_BUCKET
from bikechance_ml.jobs import build_composite as job
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import composite, registry
from bikechance_ml.models import gates as gate_tables
from tests import composite_fixture as fixture
from tests import test_composite

NOW: Final[datetime] = datetime(2026, 10, 13, 1, 30, tzinfo=UTC)


def _b3_last_day() -> date:
    return date.fromisoformat(max(fixture.b3().train_days))


def _day_of_samples() -> bytes:
    """学習サンプル 1 日ぶんの代わり（**両系統の本物の `build_now` の表**をつなげたもの）。"""
    tables = [test_composite.serving_table(one) for one in test_composite.SYSTEMS]
    return to_parquet_bytes(pa.concat_tables(tables))


@dataclass
class FakeStorage:
    """Storage と登録簿の代役。**置いた順と登録した行を覚える。**"""

    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    rows: dict[str, registry.Registered] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    registered: list[Mapping[str, object]] = field(default_factory=list)

    def download(self, bucket: str, path: str) -> bytes | None:
        return self.objects.get((bucket, path))

    def find_model(self, model_version: str) -> registry.Registered | None:
        self.events.append(f"find {model_version}")
        return self.rows.get(model_version)

    def upload(self, bucket: str, path: str, body: bytes, content_type: str) -> None:
        self.events.append(f"upload {path}")
        self.objects[(bucket, path)] = body

    def register_model_version(self, row: Mapping[str, object]) -> str:
        self.events.append(f"register {row['model_version']}")
        self.registered.append(row)
        return str(row["model_version"])


def storage(*, with_samples: bool = True) -> FakeStorage:
    """3 つの部品（と、測る日の学習サンプル）を置いた Storage。"""
    made = FakeStorage()
    bucket = registry.MODEL_BUCKET
    made.objects[(bucket, baseline_artifact.artifact_path(fixture.b3().model_version))] = (
        fixture.b3_body()
    )
    made.objects[(bucket, lightgbm_artifact.artifact_path(fixture.FOREST.model_version))] = (
        fixture.FOREST_BODY
    )
    made.objects[(bucket, gate_tables.gates_path(fixture.FOREST.model_version))] = (
        fixture.gates_body()
    )
    if with_samples:
        made.objects[(PARQUET_BUCKET, features_path(_b3_last_day()))] = _day_of_samples()
    return made


def request(**changed: object) -> job.Request:
    base: dict[str, object] = {
        "lightgbm": fixture.FOREST.model_version,
        "b3": fixture.b3().model_version,
        "label": None,
        "dropped": (),
        "measure_day": None,
        "skip_border": False,
        "out": None,
        "card": None,
        "upload": False,
        "register": False,
        "local": None,
    }
    options = argparse.Namespace(**{**base, **changed})
    return job.Request(**vars(options))


# ── 削るセルの書き方 ─────────────────────────────────────────────
def test_drops_are_read_one_per_line() -> None:
    """**1 行 1 セル**（`#` から後は注記）。重複は 1 つにし、昇順に並べる。"""
    lines = [
        "# shadow の 4 日で悪かったセル",
        "hellocycling dock 45 6-10  # 10/10〜10/12",
        "",
        "hellocycling bike 30 0",
        "hellocycling dock 45 6-10",
    ]
    assert job.parse_drops(lines) == (
        ("hellocycling", "bike", 30, "0"),
        ("hellocycling", "dock", 45, "6-10"),
    )


@pytest.mark.parametrize(
    "line",
    [
        "hellocycling bike 30",
        "hellocycling bike 30 0 extra",
        "hellocycling car 30 0",
        "hellocycling bike 31 0",
        "hellocycling bike thirty 0",
        "hellocycling bike 30 7",
    ],
)
def test_a_malformed_drop_line_is_refused(line: str) -> None:
    with pytest.raises(job.OptionError, match="1 行目"):
        job.parse_drops([line])


# ── 指定 ──────────────────────────────────────────────────────
def _options(**changed: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "lightgbm": "lgbm-v1-20261008",
        "b3": "baseline-b3-v0-20261012",
        "label": None,
        "drop_file": None,
        "measure_day": None,
        "skip_border": False,
        "out": None,
        "card": None,
        "upload": False,
        "register": False,
        "local": None,
    }
    return argparse.Namespace(**{**base, **changed})


@pytest.mark.parametrize(
    "changed",
    [
        {"label": "Rehearsal"},
        {"label": "re-hearsal"},
        {"register": True, "card": "card.md"},
        {"register": True, "upload": True},
        {"register": True, "upload": True, "card": "card.md", "skip_border": True},
    ],
)
def test_options_that_cannot_hold_are_refused(changed: dict[str, object]) -> None:
    """**登録はカードと境目の差を伴う**（CLAUDE.md §6、完了条件 3）。印は `lgbm-v1` と同じ規則。"""
    with pytest.raises(job.OptionError):
        job.request_of(_options(**changed))


def test_the_drop_file_is_read_into_the_request(tmp_path: Path) -> None:
    drops = tmp_path / "drop.txt"
    drops.write_text("hellocycling dock 45 6-10\n")
    made = job.request_of(_options(drop_file=str(drops), measure_day="2026-10-12"))
    assert made.dropped == (("hellocycling", "dock", 45, "6-10"),)
    assert made.measure_day == date(2026, 10, 12)


# ── 組む ──────────────────────────────────────────────────────
def test_the_parts_go_in_as_they_were_placed() -> None:
    """**部品は置いてあったバイト列のまま**（合成器の B3 ＝ shadow に置く B3。W6-25）。"""
    made = job.make(storage(), request(), NOW)
    assert made.artifact.parts[composite.B3_PART] == fixture.b3_body()
    assert made.artifact.parts[composite.LIGHTGBM_PART] == fixture.FOREST_BODY
    assert composite.from_bytes(made.body).parts == made.artifact.parts


def test_the_name_comes_from_the_last_b3_day() -> None:
    plain = job.make(storage(), request(skip_border=True), NOW)
    marked = job.make(storage(), request(skip_border=True, label="rehearsal"), NOW)
    day = _b3_last_day().strftime("%Y%m%d")
    assert (plain.version, marked.version) == (
        f"composite-v1-{day}",
        f"composite-v1-rehearsal-{day}",
    )


def test_a_missing_part_stops() -> None:
    port = storage()
    del port.objects[(registry.MODEL_BUCKET, gate_tables.gates_path(fixture.FOREST.model_version))]
    with pytest.raises(job.MissingPartError, match="置かれていません"):
        job.make(port, request(), NOW)


def test_a_part_that_names_another_version_stops() -> None:
    """**ファイルの中の版が指定と違えば止める**（置き間違えた B3 を使わない）。"""
    port = storage()
    other = "baseline-b3-v0-29991231"
    port.objects[(registry.MODEL_BUCKET, baseline_artifact.artifact_path(other))] = (
        fixture.b3_body()
    )
    with pytest.raises(job.MissingPartError, match="指定と違います"):
        job.make(port, request(b3=other), NOW)


def test_a_serving_name_is_refused_before_measuring() -> None:
    """**配信中の名前では置かない**（契約 38）。**境目の差を測る前に止まる。**"""
    port = storage(with_samples=False)
    version = f"composite-v1-{_b3_last_day():%Y%m%d}"
    port.rows[version] = registry.Registered(version, "composite", "v4", "composite/x", "active")
    with pytest.raises(registry.ServingVersionError):
        job.make(port, request(upload=True), NOW)


# ── 境目の差（完了条件 3）────────────────────────────────────────
def test_the_border_is_measured_on_the_last_b3_day() -> None:
    made = job.make(storage(), request(), NOW)
    assert made.border is not None
    walked = sum(sum(one.values()) for one in test_composite.EXPECTED_WALKS.values())
    assert made.border.walked.rows == walked
    assert made.border.walked.largest is not None and made.border.walked.largest > 0


def test_a_border_needs_its_day_of_samples() -> None:
    with pytest.raises(job.MissingPartError, match="features/"):
        job.make(storage(with_samples=False), request(), NOW)


# ── 書く・置く・登録する ─────────────────────────────────────────
def test_publishing_places_then_registers(tmp_path: Path) -> None:
    """**置く → 登録**。置く直前にも登録簿を引き、登録は candidate だけ（0038）。"""
    port = storage()
    card = str(tmp_path / "{version}.md")
    asked = request(upload=True, register=True, card=card, out=str(tmp_path / "{version}.json.gz"))
    made = job.make(port, asked, NOW)
    job.write_outputs(asked, made)
    job.publish(port, asked, made)
    path = composite.artifact_path(made.version)
    assert [one for one in port.events if not one.startswith("find")] == [
        f"upload {path}",
        f"register {made.version}",
    ]
    assert port.objects[(registry.MODEL_BUCKET, path)] == made.body
    assert (tmp_path / f"{made.version}.json.gz").read_bytes() == made.body
    assert (tmp_path / f"{made.version}.md").exists()


def test_the_registration_is_a_candidate_with_the_union_of_days(tmp_path: Path) -> None:
    port = storage()
    made = job.make(port, request(), NOW)
    row = job.to_registration(made, str(tmp_path / "card.md"))
    assert (row["kind"], row["status"]) == ("composite", "candidate")
    assert row["train_days"] == list(made.artifact.train_days)
    assert row["artifact_path"] == composite.artifact_path(made.version)
    metrics = row["metrics"]
    assert isinstance(metrics, dict)
    assert set(metrics) == {
        "format_version",
        "sha256",
        "bytes",
        "components",
        "cells",
        "dropped",
        "border",
    }
    assert metrics["sha256"] == composite.sha256_of(made.body)


def test_nothing_is_placed_without_upload() -> None:
    port = storage()
    asked = request()
    job.publish(port, asked, job.make(port, asked, NOW))
    assert not [one for one in port.events if one.startswith(("upload", "register"))]


def test_the_card_tells_the_parts_cells_border_and_serving() -> None:
    made = job.make(storage(), request(), NOW)
    card = job.render_card(made)
    for needed in (
        made.version,
        fixture.FOREST.model_version,
        fixture.b3().model_version,
        "## 門（セル単位",
        "## 境目の差",
        "森を歩いた行",
        "全セル B3",
        "W6-11",
    ):
        assert needed in card
