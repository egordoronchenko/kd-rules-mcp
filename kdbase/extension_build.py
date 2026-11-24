"""Сборка расширения 1С (.cfe) из XML-исходников на одноразовой копии файловой базы.

Команды (`uv run python kdbase/extension_build.py …`):

- `build --base <файловая база> [--source <каталог XML>] [--out <файл .cfe>]` — копирует только
  `1Cv8.1CD` базы в `kdbase\\run\\ext-…`, в копии пакетным конфигуратором загружает расширение из
  файлов (`/LoadConfigFromFiles … -Extension`), проверяет, что его можно применить к конфигурации
  базы (`/CheckCanApplyConfigurationExtensions`), выгружает обратно в файлы и сверяет с исходниками,
  сохраняет `.cfe` (`/DumpCfg … -Extension`) и рядом паспорт `<файл>.json` (имя, версия, режим
  совместимости, отпечатки исходников и `.cfe`). Копия удаляется в любом случае; исходная база не
  открывается.
- `verify --cfe <файл .cfe> [--source <каталог XML>]` — паспорт есть, `.cfe` и исходники не
  менялись после сборки. Этим шагом `scripts/publish.py --attach-cfe` не даёт приложить к релизу
  устаревшую сборку.

База — любая файловая с конфигурацией, где есть заимствуемые объекты (для расширения загрузки
правил — БСП 3.1.12 и новее). Платформа — `--platform`, `KD2_1CV8`, `onec_platform` в
projects.local.yaml или последняя установленная в `Program Files\\1cv8`; версия платформы не ниже
версии формата выгрузки (атрибут `version` в XML). Пользователь базы — `--user`, пароль — переменная
`KD2_IB_PASSWORD`.

Коды выхода: 0 — собрано (проверено); 1 — ошибка команды или среды; 2 — выгрузка из базы не
совпала с исходниками (`build`) или сборка устарела (`verify`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from lxml import etree

from kd_rules_mcp.console import utf8_stdout
from kd_rules_mcp.projects import load_local

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extensions" / "kd2-rules-file-load" / "src"
OUT = ROOT / "kdbase" / "build" / "kd2-rules-file-load.cfe"
RUN_ROOT = ROOT / "kdbase" / "run"
MD = "http://v8.1c.ru/8.3/MDClasses"
SCHEMA = "extension-build/1"
TIMEOUT_S = 900

Runner = Callable[[Sequence[str]], int]


class BuildError(Exception):
    """Остановка сборки: причина для протокола."""


def run_command(argv: Sequence[str]) -> int:
    """Конфигуратор без оболочки и окон; пароль не печатается (вывод команды не читается)."""
    return subprocess.run(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=TIMEOUT_S,
        check=False,
    ).returncode


def platform_path() -> Path:
    """`1cv8.exe`: `KD2_1CV8`, `onec_platform` личного файла или последняя установленная."""
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
        raise BuildError("Не найдена платформа 1С: задайте --platform, KD2_1CV8 или onec_platform")
    return max(found, key=lambda p: tuple(int(x) for x in re.findall(r"\d+", p.parents[1].name)))


def read_extension(source: Path) -> dict[str, str]:
    """Имя, версия и режим совместимости из `Configuration.xml` исходников."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.parse(str(source / "Configuration.xml"), parser).getroot()
    props = root.find(f"{{{MD}}}Configuration/{{{MD}}}Properties")
    if props is None or props.findtext(f"{{{MD}}}ObjectBelonging") != "Adopted":
        raise BuildError("Configuration.xml — не расширение конфигурации")
    name = props.findtext(f"{{{MD}}}Name") or ""
    if not name:
        raise BuildError("В Configuration.xml нет имени расширения")
    return {
        "extension": name,
        "version": props.findtext(f"{{{MD}}}Version") or "",
        "compatibility_mode": props.findtext(f"{{{MD}}}ConfigurationExtensionCompatibilityMode")
        or "",
    }


def source_files(source: Path) -> dict[str, bytes]:
    """Файлы исходников по относительным путям; служебный `ConfigDumpInfo.xml` не в счёт."""
    return {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in sorted(source.rglob("*"))
        if p.is_file() and p.name != "ConfigDumpInfo.xml"
    }


def sources_digest(source: Path) -> str:
    """Отпечаток исходников: пути и байты всех файлов в порядке путей."""
    digest = hashlib.sha256()
    for path, content in source_files(source).items():
        digest.update(path.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized_xml(content: bytes) -> object:
    """Структура XML без оформления: порядок пространств имён и отступы конфигуратор меняет."""
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(content, parser)

    def shape(element: etree._Element) -> object:
        return [
            element.tag,
            sorted(element.attrib.items()),
            (element.text or "").strip(),
            [shape(child) for child in element],
        ]

    return shape(root)


def compare_dump(source: Path, dumped: Path) -> list[str]:
    """Разошедшиеся пути: XML — по структуре, модули — с точностью до BOM и переводов строк."""
    before, after = source_files(source), source_files(dumped)
    mismatches = sorted(before.keys() ^ after.keys())
    for path in sorted(before.keys() & after.keys()):
        left, right = before[path], after[path]
        if path.endswith(".xml"):
            same = normalized_xml(left) == normalized_xml(right)
        else:
            same = _text(left) == _text(right)
        if not same:
            mismatches.append(path)
    return mismatches


def _text(content: bytes) -> str:
    return content.decode("utf-8-sig").replace("\r\n", "\n").rstrip("\n")


class BaseCopy:
    """Одноразовая копия файловой базы (только `1Cv8.1CD`) и запуски пакетного конфигуратора в ней.

    Каталог копии создаётся в `run_root` и удаляется в `close`; исходная база не открывается.
    """

    def __init__(
        self,
        base: Path,
        platform: Path,
        *,
        user: str,
        password: str,
        run_root: Path,
        runner: Runner,
    ) -> None:
        self.run_root = run_root.resolve()
        self.runner = runner
        self.steps: list[dict[str, object]] = []
        self.task_dir: Path | None = None
        base = base.resolve(strict=True)
        if not (base / "1Cv8.1CD").is_file():
            raise BuildError("Нужна файловая база: в каталоге нет 1Cv8.1CD")
        if self.run_root == base or self.run_root.is_relative_to(base):
            raise BuildError("Рабочая папка не должна лежать внутри исходной базы")
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.task_dir = Path(tempfile.mkdtemp(prefix="ext-", dir=self.run_root)).resolve()
        copy = self.task_dir / "ib"
        copy.mkdir()
        shutil.copy2(base / "1Cv8.1CD", copy / "1Cv8.1CD")
        self.common = [str(platform), "DESIGNER", "/F", str(copy)]
        if user:
            self.common += ["/N", user]
        if password:
            self.common += ["/P", password]

    @property
    def folder(self) -> Path:
        if self.task_dir is None:
            raise BuildError("Копия базы уже удалена")
        return self.task_dir

    def run(self, label: str, action: Sequence[str]) -> bool:
        """Один запуск конфигуратора; при ошибке в протокол идёт хвост его журнала."""
        result_path = self.folder / f"{label}.result"
        log_path = self.folder / f"{label}.log"
        code = self.runner(
            [
                *self.common,
                *action,
                "/DisableStartupDialogs",
                "/Out",
                str(log_path),
                "/DumpResult",
                str(result_path),
            ]
        )
        result = (
            result_path.read_text(encoding="utf-8-sig").strip()
            if result_path.is_file()
            else "missing"
        )
        step: dict[str, object] = {"step": label, "exit_code": code, "result": result}
        self.steps.append(step)
        if code != 0 or result != "0":
            if log_path.is_file():
                step["log"] = _log_tail(log_path)
            return False
        return True

    def close(self) -> None:
        if self.task_dir is None:
            return
        if self.task_dir.parent != self.run_root or not self.task_dir.name.startswith("ext-"):
            raise RuntimeError("Небезопасный путь удаления копии базы")
        shutil.rmtree(self.task_dir, ignore_errors=True)
        self.task_dir = None


ERRORS = (OSError, ValueError, BuildError, etree.XMLSyntaxError, subprocess.TimeoutExpired)


def build(
    base: Path,
    source: Path,
    out: Path,
    platform: Path,
    *,
    user: str = "",
    password: str = "",
    dump_to: Path | None = None,
    run_root: Path = RUN_ROOT,
    runner: Runner = run_command,
) -> tuple[int, dict[str, object]]:
    """Сборка на копии базы; возвращает код выхода и протокол (без пароля).

    `dump_to` — куда положить выгрузку расширения из базы (каноническая форма исходников).
    """
    report: dict[str, object] = {"schema_version": SCHEMA}
    session: BaseCopy | None = None
    try:
        info = read_extension(source)
        report.update(info)
        session = BaseCopy(
            base, platform, user=user, password=password, run_root=run_root, runner=runner
        )
        report["steps"] = session.steps
        dumped = session.folder / "dumped"
        cfe = session.folder / "extension.cfe"
        name = info["extension"]
        actions = (
            ("load", ["/LoadConfigFromFiles", str(source.resolve()), "-Extension", name]),
            ("applicability", ["/CheckCanApplyConfigurationExtensions", "-Extension", name]),
            ("dump_files", ["/DumpConfigToFiles", str(dumped), "-Extension", name]),
            ("dump_cfe", ["/DumpCfg", str(cfe), "-Extension", name]),
        )
        for label, action in actions:
            if not session.run(label, action):
                return 1, report
        if dump_to is not None:
            shutil.copytree(dumped, dump_to, dirs_exist_ok=True)
        mismatches = compare_dump(source, dumped)
        report["dump_equal"] = not mismatches
        report["mismatches"] = mismatches
        if mismatches:
            return 2, report
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cfe, out)
        passport = {
            "schema_version": SCHEMA,
            **info,
            "sources_sha256": sources_digest(source),
            "cfe_sha256": file_digest(out),
            "cfe_size": out.stat().st_size,
            "platform": platform.parents[1].name if len(platform.parents) > 1 else "",
        }
        _passport_path(out).write_text(
            json.dumps(passport, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        report.update({"cfe": str(out), "cfe_sha256": passport["cfe_sha256"]})
        return 0, report
    except ERRORS as error:
        report["error"] = f"{type(error).__name__}: {error}"
        return 1, report
    finally:
        if session is not None:
            session.close()


def check(
    base: Path,
    cfe: Path,
    platform: Path,
    *,
    user: str = "",
    password: str = "",
    run_root: Path = RUN_ROOT,
    runner: Runner = run_command,
) -> tuple[int, dict[str, object]]:
    """Готовый `.cfe` загружается в копию другой базы и проверяется на применимость.

    Так один и тот же файл проверяется на нескольких конфигурациях (БП, ЗУП, ERP…).
    """
    report: dict[str, object] = {"schema_version": SCHEMA}
    session: BaseCopy | None = None
    try:
        passport = json.loads(_passport_path(cfe).read_text(encoding="utf-8"))
        name = str(passport["extension"])
        report.update({"extension": name, "cfe_sha256": file_digest(cfe)})
        if passport.get("cfe_sha256") != report["cfe_sha256"]:
            raise BuildError("Файл .cfe не совпадает с паспортом сборки")
        session = BaseCopy(
            base, platform, user=user, password=password, run_root=run_root, runner=runner
        )
        report["steps"] = session.steps
        for label, action in (
            ("load_cfe", ["/LoadCfg", str(cfe.resolve()), "-Extension", name]),
            ("applicability", ["/CheckCanApplyConfigurationExtensions", "-Extension", name]),
        ):
            if not session.run(label, action):
                return 1, report
        return 0, report
    except (*ERRORS, KeyError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
        return 1, report
    finally:
        if session is not None:
            session.close()


def verify(cfe: Path, source: Path) -> tuple[int, dict[str, object]]:
    """Сборка соответствует текущим исходникам и не менялась после сборки."""
    passport_path = _passport_path(cfe)
    if not cfe.is_file() or not passport_path.is_file():
        return 1, {"error": f"Нет {cfe.name} или паспорта {passport_path.name}: сначала build"}
    passport = json.loads(passport_path.read_text(encoding="utf-8"))
    problems = []
    if passport.get("cfe_sha256") != file_digest(cfe):
        problems.append("файл .cfe изменён после сборки")
    if passport.get("sources_sha256") != sources_digest(source):
        problems.append("исходники изменились после сборки — пересоберите")
    if passport.get("extension") != read_extension(source)["extension"]:
        problems.append("в паспорте другое расширение")
    return (2 if problems else 0), {**passport, "problems": problems}


def _passport_path(cfe: Path) -> Path:
    return cfe.with_name(cfe.name + ".json")


def _log_tail(path: Path, lines: int = 20) -> list[str]:
    """Последние строки журнала конфигуратора (кодировку платформа выбирает сама)."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()][-lines:]


def main(argv: Sequence[str] | None = None) -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(description="Сборка расширения 1С (.cfe) из XML-исходников")
    commands = parser.add_subparsers(dest="command", required=True)
    build_cmd = commands.add_parser("build", help="собрать .cfe на копии файловой базы")
    build_cmd.add_argument("--base", type=Path, required=True, help="каталог файловой базы")
    build_cmd.add_argument("--source", type=Path, default=SOURCE)
    build_cmd.add_argument("--out", type=Path, default=OUT)
    build_cmd.add_argument("--dump-to", type=Path, help="куда положить выгрузку из базы")
    check_cmd = commands.add_parser("check", help="проверить готовый .cfe на другой базе")
    check_cmd.add_argument("--base", type=Path, required=True, help="каталог файловой базы")
    check_cmd.add_argument("--cfe", type=Path, default=OUT)
    for command in (build_cmd, check_cmd):
        command.add_argument("--platform", type=Path)
        command.add_argument("--user", default="")
    verify_cmd = commands.add_parser("verify", help="сборка соответствует исходникам")
    verify_cmd.add_argument("--cfe", type=Path, default=OUT)
    verify_cmd.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args(argv)
    if args.command == "verify":
        code, report = verify(args.cfe, args.source)
    else:
        try:
            platform = args.platform or platform_path()
        except BuildError as error:
            print(error, file=sys.stderr)
            return 1
        password = os.environ.get("KD2_IB_PASSWORD", "")
        if args.command == "build":
            code, report = build(
                args.base,
                args.source,
                args.out,
                platform,
                user=args.user,
                password=password,
                dump_to=args.dump_to,
            )
        else:
            code, report = check(args.base, args.cfe, platform, user=args.user, password=password)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
