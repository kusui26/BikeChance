"""下限の候補を 1 日の行で測る道具（`jobs/measure_floor.py`。W6 の PR A）。

**判定は `eval/b2_effect.py` が持つ**（`test_eval_b2_effect.py` で留める）。ここで見るのは
道具の口：**候補の版が本当に k 日の版かを先に確かめる**・行を到着の曜日種別で絞る・
参考の版は並べるだけで判定に使わない・**読むだけで Storage を開かずに測れる**（`--local`）。
"""

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.baselines import climatology
from bikechance_ml.baselines.artifact import artifact_path, to_bytes
from bikechance_ml.features.constants import FEATURE_SET
from bikechance_ml.features.grid import features_path
from bikechance_ml.jobs import measure_floor
from bikechance_ml.jobs.measure_floor import InputError, parse_candidates, rows_to_measure, run
from bikechance_ml.models import registry
from tests import eval_fixture as fixture
from tests.test_eval_b2_effect import EVAL_ROWS, INFORMATIVE, informative_artifact

DAY0, DAY1, DAY2 = fixture.DAYS

#: 報告書の K の行（`_verdict`）。**9/28 の朝に読むのはここ。**
K_IS_3: Final[str] = "**K ＝ 3**"
K_IS_NOT_SERVED: Final[str] = "**K ＝ 配らない**"


def _must_not_open(config: object) -> None:
    raise AssertionError("Storage を開いた（--local だけなら開かないはず）")


#: 土曜に着く 1 行。**平日で測るときに外れる行**（候補と参考が同じ行を見ているかを見分ける）。
SATURDAY_ROW: Final = fixture.to_table(
    [fixture.row(DAY2, "hellocycling", "a", 5, 1, 1, 1, 1, dow_type="sat")]
)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`features/date=DAY2`（平日 6 行と土曜 1 行）と、候補の版（k = 3）・B2 の引けない版。"""
    monkeypatch.setattr(measure_floor, "read_storage_config", lambda: None)
    monkeypatch.setattr(measure_floor, "open_storage", _must_not_open)
    path = tmp_path / features_path(DAY2)
    path.parent.mkdir(parents=True)
    pq.write_table(pa.concat_tables([EVAL_ROWS, SATURDAY_ROW]), path)
    (tmp_path / "k3.json.gz").write_bytes(to_bytes(INFORMATIVE))
    never = informative_artifact(climatology.DayFloor.uniform(99))
    (tmp_path / "never.json.gz").write_bytes(to_bytes(never))
    return tmp_path


def measure(root: Path, *extra: str, day: str = f"{DAY2}", dow: str = "weekday") -> int:
    return run(["--day", day, "--dow", dow, "--local", str(root), *extra])


# ── 候補の渡し方 ──────────────────────────────────────────────
def test_candidates_are_k_equals_path() -> None:
    assert parse_candidates(["3=a.json.gz", "5=b.json.gz"]) == {
        3: Path("a.json.gz"),
        5: Path("b.json.gz"),
    }
    assert parse_candidates([]) == {}, "参考の版だけを測るときは候補が無い"


def test_nothing_to_measure_is_refused(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """**候補も参考も無ければ止める**（空の報告書で K を出さない）。"""
    assert measure(root) == 2
    assert "測るものが無い" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("values", "why"),
    [
        (["a.json.gz"], "k=パス"),
        (["three=a.json.gz"], "k=パス"),
        (["3="], "k=パス"),
        (["3=a.json.gz", "3=b.json.gz"], "2 度ある"),
    ],
)
def test_malformed_candidates_are_refused(values: list[str], why: str) -> None:
    """**同じ k を 2 度渡せない**（どちらで測ったか分からなくなる）。"""
    with pytest.raises(InputError, match=why):
        parse_candidates(values)


# ── 測って書く ────────────────────────────────────────────────
def test_the_report_names_k_and_the_numbers_behind_it(root: Path) -> None:
    """**全体の表 → K → 組ごとの表**。Storage を開かずに測れる（`--local`）。"""
    out = root / "report.md"
    assert measure(root, "--candidate", f"3={root / 'k3.json.gz'}", "--out", str(out)) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith(f"# B2 の下限の測り（{DAY2}、到着が weekday の行）")
    assert f"| k = 3（{INFORMATIVE.model_version}、k3.json.gz） |" in text
    assert K_IS_3 in text
    assert "## 組ごと" in text
    assert text.index(K_IS_3) < text.index("## 組ごと")


def test_the_report_goes_to_stdout_without_out(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert measure(root, "--candidate", f"3={root / 'k3.json.gz'}") == 0
    captured = capsys.readouterr()
    assert K_IS_3 in captured.out
    assert K_IS_3 in captured.err, "K は標準エラーにも 1 行出す（流れる出力の最後で読める）"


def test_a_candidate_that_did_not_help_leaves_k_not_served(root: Path) -> None:
    """**効いた候補が無ければ配らない。** B2 が 1 セルも引けない版は「落とすと 0」。"""
    out = root / "report.md"
    never = root / "never.json.gz"
    assert measure(root, "--dow", "all", "--candidate", f"3={never}", "--out", str(out)) == 0
    assert K_IS_NOT_SERVED in out.read_text(encoding="utf-8")


def test_the_reference_is_listed_but_not_judged(root: Path) -> None:
    """**参考の版（配信中の版など）は並べるだけ。** K は候補だけから選ぶ。"""
    out = root / "report.md"
    reference = ["--reference-file", str(root / "k3.json.gz")]
    never = f"3={root / 'never.json.gz'}"
    assert measure(root, "--dow", "all", "--candidate", never, *reference, "--out", str(out)) == 0
    text = out.read_text(encoding="utf-8")
    assert f"| 参考：{INFORMATIVE.model_version}（k3.json.gz） |" in text
    assert "（判定に使わない）" in text
    assert K_IS_NOT_SERVED in text, "参考の版が効いても、K は候補で決まる"


def test_the_reference_is_measured_on_the_same_rows(root: Path) -> None:
    """**参考も候補と同じ行に当てる**（平日で測れば、土曜に着く行は両方から外れる）。"""
    out = root / "report.md"
    reference = ["--reference-file", str(root / "never.json.gz")]
    candidate = f"3={root / 'k3.json.gz'}"
    assert measure(root, "--candidate", candidate, *reference, "--out", str(out)) == 0
    summary = out.read_text(encoding="utf-8").split("## 組ごと")[0].splitlines()
    counted = [line.split("|")[2].strip() for line in summary if line.startswith(("| k", "| 参考"))]
    weekday_rows = EVAL_ROWS.num_rows * 2  # 2 ターゲット
    assert counted == [f"{weekday_rows:,}", f"{weekday_rows:,}"]


def test_a_reference_alone_is_measured_without_choosing_k(root: Path) -> None:
    """**参考の版だけでも測れる**（配っている版を同じ物差しで測り直す。W6 プラン §13.7）。"""
    out = root / "report.md"
    reference = ["--reference-file", str(root / "k3.json.gz")]
    assert measure(root, *reference, "--out", str(out)) == 0
    text = out.read_text(encoding="utf-8")
    assert f"| 参考：{INFORMATIVE.model_version}（k3.json.gz） |" in text
    assert "候補が無いので K は選ばない" in text
    assert "**K ＝" not in text


# ── 参考の版を登録簿から読む（9/28 の使い方）──────────────────
@dataclass
class Models:
    """登録簿と `models` バケットの代役。**読むだけ**（学習サンプルは `--local` から読む）。"""

    rows: dict[str, registry.Registered] = field(default_factory=dict)
    bodies: dict[str, bytes] = field(default_factory=dict)

    def active_model(self) -> registry.Registered | None:
        return None

    def find_model(self, model_version: str) -> registry.Registered | None:
        return self.rows.get(model_version)

    def download(self, bucket: str, path: str) -> bytes | None:
        return self.bodies.get(path) if bucket == registry.MODEL_BUCKET else None


def _registered(models: Models, status: str = "active") -> Models:
    name = INFORMATIVE.model_version
    path = artifact_path(name)
    models.rows[name] = registry.Registered(
        model_version=name,
        kind=registry.BASELINE_KIND,
        feature_set=FEATURE_SET,
        artifact_path=path,
        status=status,
    )
    models.bodies[path] = to_bytes(INFORMATIVE)
    return models


def _open_models(monkeypatch: pytest.MonkeyPatch, models: Models) -> None:
    monkeypatch.setattr(measure_floor, "open_storage", lambda config: nullcontext(models))


def test_the_reference_from_the_registry_names_its_status(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**登録簿の版は状態も添える**（候補の k = 5 と配信中の版は、学習窓が同じで名前が重なる）。"""
    _open_models(monkeypatch, _registered(Models()))
    out = root / "report.md"
    reference = ["--reference-version", INFORMATIVE.model_version]
    assert (
        measure(root, "--candidate", f"3={root / 'k3.json.gz'}", *reference, "--out", str(out)) == 0
    )
    text = out.read_text(encoding="utf-8")
    assert f"| 参考：{INFORMATIVE.model_version}（登録簿の active） |" in text
    assert f"| k = 3（{INFORMATIVE.model_version}、k3.json.gz） |" in text


def test_an_unknown_reference_version_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _open_models(monkeypatch, Models())
    assert measure(root, "--reference-version", "baseline-b3-v0-20990101") == 2
    assert "登録されていない版" in capsys.readouterr().err


def test_a_reference_without_its_artifact_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    models = _registered(Models())
    models.bodies.clear()
    _open_models(monkeypatch, models)
    assert measure(root, "--reference-version", INFORMATIVE.model_version) == 2
    assert "成果物がありません" in capsys.readouterr().err


# ── 測れないときは止める ──────────────────────────────────────
def test_a_candidate_of_another_thickness_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**違う版で測って K を決めない。** 平日 3 日の版を k = 4 として渡すと止まる。"""
    assert measure(root, "--candidate", f"4={root / 'k3.json.gz'}") == 2
    assert "候補の版が k = 4 になっていない" in capsys.readouterr().err


def test_the_candidates_are_checked_before_the_rows_are_read(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**行を読む前に候補を確かめる**（違う版なら、Storage から 1 日ぶんを落とす前に止まる）。"""
    assert measure(root, "--candidate", f"4={root / 'k3.json.gz'}", day=f"{DAY1}") == 2
    assert "候補の版が k = 4 になっていない" in capsys.readouterr().err


def test_all_rows_skip_the_thickness_check(root: Path) -> None:
    """`--dow all` は**行を絞らず、厚さも確かめない**（EDA #8 の再現に使う）。"""
    assert measure(root, "--dow", "all", "--candidate", f"4={root / 'k3.json.gz'}") == 0


def test_a_day_without_samples_is_refused(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """**学習サンプルがまだ無い日は測らない**（`build_features` の後に測る）。"""
    assert measure(root, "--candidate", f"3={root / 'k3.json.gz'}", day=f"{DAY1}") == 2
    assert "学習サンプルがまだ無い" in capsys.readouterr().err


def test_a_day_without_rows_on_the_dow_type_is_refused() -> None:
    """**0 行で K を出さない。** 日曜・祝日に着く行が 1 つも無い日で測ろうとすると止まる。"""
    with pytest.raises(InputError, match="到着が sun_holiday の行が 1 つも無い"):
        rows_to_measure(EVAL_ROWS, "sun_holiday", DAY2)
    assert rows_to_measure(EVAL_ROWS, "weekday", DAY2).num_rows == EVAL_ROWS.num_rows


def test_a_missing_candidate_file_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert measure(root, "--candidate", f"3={root / 'k9.json.gz'}") == 2
    assert "成果物が無い" in capsys.readouterr().err
