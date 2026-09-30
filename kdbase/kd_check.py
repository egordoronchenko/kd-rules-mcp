"""Сверка файла правил обмена через базу КД 2.1.8.2 (design.md, Д11; задача 8.1).

Правила загружает штатная обработка `ЗагрузкаКонвертации` в толстом клиенте: её модуль целиком
под `#Если Клиент Тогда`, поэтому внешнее соединение (COM) для загрузки не годится. Клиент
запускается командой `1cv8 ENTERPRISE` с внешней обработкой `ПроверкаЗагрузкиПравил` (`/Execute`,
`/C`); обработка пишет протокол и завершает сеанс. EPF собирается из исходников `kdbase\\src`
командой `1cv8 DESIGNER /LoadExternalDataProcessorOrReportFromFiles`.

Команды (`uv run python kdbase/kd_check.py …`):

- `prepare` — один раз для рабочей базы `base\\`: через COM выполняет стартовое обновление КД
  (иначе клиент останавливается на окне «Изменился номер версии конфигурации») и заводит
  пользователя «Агент» с полными правами без предупреждений об опасных действиях (иначе
  `/Execute` останавливается на «Предупреждении безопасности»).
- `check <файл правил>` — копия `base\\` в `kdbase\\run\\<время>\\`, сборка EPF при изменении
  исходников, запуск, протокол в stdout; код выхода 0 — «ИТОГ OK», 1 — ошибка или таймаут.

Платформа — `1cv8.exe` из переменной `KD2_1CV8`, `onec_platform` в projects.local.yaml или последняя
установленная в `Program Files\\1cv8`. База КД — файловая ИБ в `base\\` из конфигурации
«Конвертация данных» 2.1 (создаётся вручную, в git не входит).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from kd2_rules_mcp.projects import load_local

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "base"
KDBASE = ROOT / "kdbase"
SOURCE = KDBASE / "src" / "ПроверкаЗагрузкиПравил.xml"
EPF = KDBASE / "build" / "ПроверкаЗагрузкиПравил.epf"
RUNS = KDBASE / "run"
USER = "Агент"
TIMEOUT_S = 300
DONE = "КОНЕЦ"


def _platform() -> Path:
    """Путь к `1cv8.exe`: `KD2_1CV8`, `onec_platform` личного файла или последняя установленная."""
    if os.environ.get("KD2_1CV8"):
        return Path(os.environ["KD2_1CV8"])
    local = ROOT / "projects.local.yaml"
    configured = load_local(local).onec_platform if local.is_file() else None
    if configured is not None:
        return configured
    found: list[Path] = []
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if root:
            found += Path(root, "1cv8").glob("8.3.*/bin/1cv8.exe")
    if not found:
        raise SystemExit("Не найдена платформа 1С: задайте KD2_1CV8 или onec_platform")
    return max(found, key=lambda path: _version(path.parents[1].name))


def _version(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", name))


def prepare(base: Path) -> None:
    """Стартовое обновление КД и пользователь «Агент» через внешнее соединение."""
    import win32com.client

    connector = win32com.client.Dispatch("V83.COMConnector")
    try:
        connection = connector.Connect(f'File="{base}";')
    except Exception:  # пользователи уже есть — входим под «Агентом»
        connection = connector.Connect(f'File="{base}";Usr="{USER}";')
    constant = connection.Константы.НомерВерсииКонфигурации
    if constant.Получить() != connection.Метаданные.Версия:
        connection.Обработки.ОбновлениеИнформационнойБазы.Создать().ВыполнитьОбновление()
    users = connection.ПользователиИнформационнойБазы
    user = users.НайтиПоИмени(USER)
    if user is None:
        user = users.СоздатьПользователя()
        user.Имя = USER
        user.ПолноеИмя = "Агент проверки правил"
        user.АутентификацияСтандартная = True
        user.Роли.Добавить(connection.Метаданные.Роли.ПолныеПрава)
    protection = connection.NewObject("ОписаниеЗащитыОтОпасныхДействий")
    protection.ПредупреждатьОбОпасныхДействиях = False
    user.ЗащитаОтОпасныхДействий = protection
    user.Записать()
    print(f"База {base}: версия {constant.Получить()}, пользователь «{USER}» готов")


def check(rules: Path, keep: bool = False) -> int:
    """Загрузка `rules` в копию базы КД; протокол в stdout, код выхода по итогу."""
    run_dir = RUNS / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    base = run_dir / "base"
    base.mkdir(parents=True)
    shutil.copy2(BASE / "1Cv8.1CD", base / "1Cv8.1CD")
    protocol = run_dir / "protocol.txt"
    try:
        _build_epf(base)
        client = subprocess.Popen(
            [
                str(_platform()),
                "ENTERPRISE",
                *_session(base),
                "/DisableStartupMessages",
                "/Execute",
                str(EPF),
                "/C",
                f"kd2check|{rules.resolve()}|{protocol}",
            ]
        )
        text = _wait(protocol, client.pid)
    finally:
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)
    print(text)
    return 0 if "\nИТОГ OK\n" in f"\n{text}\n" and DONE in text else 1


def _build_epf(base: Path) -> None:
    """Собирает EPF, если её нет или исходники новее."""
    sources = [SOURCE, *SOURCE.with_suffix("").rglob("*")]
    newest = max(path.stat().st_mtime for path in sources if path.is_file())
    if EPF.is_file() and EPF.stat().st_mtime >= newest:
        return
    EPF.parent.mkdir(parents=True, exist_ok=True)
    log = base.parent / "epf-build.log"
    result = subprocess.run(
        [
            str(_platform()),
            "DESIGNER",
            *_session(base),
            "/LoadExternalDataProcessorOrReportFromFiles",
            str(SOURCE),
            str(EPF),
            "/Out",
            str(log),
        ],
        check=False,
    )
    if result.returncode != 0 or not EPF.is_file():
        raise SystemExit(f"Сборка EPF: код {result.returncode}\n{_read(log)}")


def _session(base: Path) -> list[str]:
    """Файловая база под пользователем «Агент», без стартовых диалогов."""
    return ["/F", str(base), "/N", USER, "/DisableStartupDialogs"]


def _wait(protocol: Path, pid: int | None) -> str:
    """Ждёт строку «КОНЕЦ» в протоколе; по таймауту завершает клиент."""
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        text = _read(protocol)
        if DONE in text:
            _wait_exit(pid)
            return text.strip()
        time.sleep(2)
    if pid is not None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    partial = _read(protocol)
    return f"{partial}ОШИБКА таймаут {TIMEOUT_S} с: клиент КД не завершил проверку".strip()


def _read(protocol: Path) -> str:
    """Текст протокола; пока клиент держит файл на запись, Windows не даёт его прочитать."""
    try:
        return protocol.read_text(encoding="utf-8-sig", errors="replace")
    except (FileNotFoundError, PermissionError):
        return ""


def _wait_exit(pid: int | None) -> None:
    """Клиент держит файлы копии базы, пока не завершится."""
    if pid is None:
        return
    for _ in range(30):
        found = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        )
        if str(pid) not in found.stdout:
            return
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Сверка правил обмена через базу КД")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="подготовить рабочую базу base\\")
    checker = commands.add_parser("check", help="загрузить файл правил в копию базы")
    checker.add_argument("rules", type=Path)
    checker.add_argument("--keep", action="store_true", help="не удалять копию базы")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(BASE)
    else:
        sys.exit(check(args.rules, keep=args.keep))


if __name__ == "__main__":
    main()
