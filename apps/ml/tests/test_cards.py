"""置く・登録するジョブが共有する決まり（`jobs/cards.py`。`fit_lightgbm` と `build_composite`）。"""

from pathlib import Path

from bikechance_ml.jobs import cards


def test_the_version_is_filled_into_the_output_paths() -> None:
    """**版の名前は走らせる前に分からない**ので、出力先に `{version}` と書ける。"""
    written = cards.expand("../../docs/model_cards/{version}.md", "lgbm-v1-20261008")
    assert written == "../../docs/model_cards/lgbm-v1-20261008.md"
    assert cards.expand(None, "lgbm-v1-20261008") is None


def test_the_card_path_is_recorded_relative_to_the_repository(tmp_path: Path) -> None:
    """**登録簿には「どこ起点か」が分かる形で残す。**

    `--card` はシェルから見た書き出し先なので、`apps/ml` から走らせると
    `../../docs/…` になる。それをそのまま入れると読む人が辿れない。
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "model_cards").mkdir(parents=True)
    card = tmp_path / "docs" / "model_cards" / "x.md"
    deep = tmp_path / "apps" / "ml"
    deep.mkdir(parents=True)

    assert cards.card_reference(str(card)) == "docs/model_cards/x.md"
    assert cards.card_reference(f"{deep}/../../docs/model_cards/x.md") == "docs/model_cards/x.md"


def test_a_card_outside_any_repository_is_left_alone(tmp_path: Path) -> None:
    """**勝手に別の場所を指さない。** `.git` が見つからなければそのまま残す。"""
    outside = tmp_path / "loose.md"
    assert cards.card_reference(str(outside)) == str(outside)


def test_no_card_stays_none() -> None:
    assert cards.card_reference(None) is None


def test_a_label_has_no_hyphen() -> None:
    """**印にハイフンを許さない**——許すと、版の名前から印と日付を切り分けられなくなる。"""
    assert cards.LABEL_PATTERN.match("rehearsal") is not None
    assert cards.LABEL_PATTERN.match("re-hearsal") is None
    assert cards.LABEL_PATTERN.match("Rehearsal") is None
