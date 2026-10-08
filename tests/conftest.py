"""Общая настройка тестов.

Основной набор работает на синтетических данных из `tests/data` и не требует внешних файлов.
Проверки на внешних данных включаются переменными окружения, без них тесты пропускаются:

- `KD2_CORPUS_DIRS` — каталоги XML-выгрузок конфигураций с макетами правил в `ExchangePlans`
  (корпус, `tests/corpus.py`);
- `KD2_REFERENCE_DIR` — XML-выгрузка конфигурации «Конвертация данных 2.1» (эталон формата);
- `KD2_DEPLOY_URL` — адрес развёрнутого сервера (`tests/test_deploy.py`);
- `KD2_KDBASE_CHECK`, `KD2_BSP_PROJECT`, `KD2_BSP_BASE`, `KD2_BSP_PLAN` — сверка через базы 1С
  (`tests/test_kdbase.py`).

Значения по умолчанию, если переменная не задана: корпус — выгрузки конфигураций проектов из
`projects.yaml` с папками из `projects.local.yaml`, эталон — `reference/kd2-cfg`, если он есть.
Так на машине с настроенными проектами корпус подключается без переменных.
"""

import os
from pathlib import Path

import pytest

from kd_rules_mcp.projects import load_catalog, load_local, resolve

ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config: pytest.Config) -> None:
    """Корпус и эталон по умолчанию — из настроек проектов и `reference/`."""
    if "KD2_CORPUS_DIRS" not in os.environ:
        dirs = _project_dumps()
        if dirs:
            os.environ["KD2_CORPUS_DIRS"] = os.pathsep.join(str(path) for path in dirs)
    reference = ROOT / "reference" / "kd2-cfg"
    if "KD2_REFERENCE_DIR" not in os.environ and reference.is_dir():
        os.environ["KD2_REFERENCE_DIR"] = str(reference)
    # До импорта тестов: читатели ещё не привязаны в модулях сервиса.
    from tests.session_inputs import install

    install()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Удаляет временные копии структур корпуса."""
    from tests.session_inputs import cleanup

    cleanup()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Метка `slow` на тесте сервиса пилота: файл правит параллельная задача."""
    slow = pytest.mark.slow
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if "::test_service_pilot" in nodeid:
            item.add_marker(slow)


def _project_dumps() -> list[Path]:
    """Выгрузки основных конфигураций проектов, чьи папки заданы на этой машине."""
    catalog_file, local_file = ROOT / "projects.yaml", ROOT / "projects.local.yaml"
    if not (catalog_file.is_file() and local_file.is_file()):
        return []
    catalog, local = load_catalog(catalog_file), load_local(local_file)
    dumps: list[Path] = []
    for project_id, folder in local.project_dirs.items():
        project = catalog.projects.get(project_id)
        if project is None:
            continue
        for configuration in project.configurations.values():
            path = resolve(folder, configuration.dump)
            if path not in dumps:
                dumps.append(path)
    return dumps
