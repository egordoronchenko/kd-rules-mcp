"""Проверка перечня корпуса макетов правил (`KD2_CORPUS_DIRS`)."""

import pytest

from tests.corpus import CORPUS_ENV, corpus_dirs, corpus_files, missing_dirs

pytestmark = pytest.mark.corpus


def test_corpus_dirs_have_rules() -> None:
    """Каждый заданный каталог — выгрузка с `ExchangePlans`, и в корпусе есть макеты."""
    if not corpus_dirs():
        pytest.skip(f"Корпус не задан: переменная {CORPUS_ENV} пуста")
    missing = missing_dirs()
    assert not missing, f"Нет каталога ExchangePlans: {', '.join(map(str, missing.values()))}"
    files = corpus_files()
    assert files
    assert len({item.id for item in files}) == len(files)
