"""Круг XML расширения на одноразовой копии файловой базы, без серверных баз.

Запуск: uv run python kdbase/ed_registration_kit_check.py --base <папка>
--kit <папка комплекта> --platform <1cv8.exe> [--user <пользователь>].
Протокол содержит только коды, версии и хеши; тексты учебной базы не сохраняются.
Коды выхода: 0 — круг совпал, 1 — ошибка команды/среды, 2 — расхождение XML.
Команды соответствуют authoring/ed/templates/manager_instruction.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from lxml import etree

from kd_rules_mcp.authoring.ed.manifest import json_bytes, sha256
from kd_rules_mcp.authoring.ed.registration_delivery import RegistrationManifest
from kd_rules_mcp.authoring.ed.xml_dump import M
from kd_rules_mcp.errors import RegistrationDeliveryError

Runner = Callable[[Sequence[str]], int]
RUN_ROOT = Path(__file__).resolve().parent / "run"


def file_hash(path: Path) -> str:
    """Файловая база может быть больше доступной памяти; читаем потоково."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_command(argv: Sequence[str]) -> int:
    """Без оболочки, видимых окон, печати параметров и содержимого журналов."""
    return subprocess.run(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=600,
        check=False,
    ).returncode


def normalized_xml(content: bytes) -> bytes:
    """Сравнивает структуру, порядок, значения и UUID, игнорируя только оформление."""
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(content, parser)
    if getattr(root.getroottree().docinfo, "doctype", "") or any(
        isinstance(n, etree._Entity) for n in root.iter()
    ):
        raise ValueError("DTD запрещён")

    # Пространства имён конфигуратор упорядочивает иначе. URI и значения не маскируем.
    def shape(element: etree._Element) -> object:
        return [
            element.tag,
            sorted(element.attrib.items()),
            element.text or "",
            [shape(c) for c in element],
        ]

    return json_bytes(shape(root))


def compare_extension(expected: Path, actual: Path) -> dict[str, object]:
    """Каждый XML должен вернуться; добавленные объекты/модули тоже расхождение."""
    before = {
        p.relative_to(expected).as_posix(): p.read_bytes()
        for p in expected.rglob("*")
        if p.is_file()
    }
    after = {
        p.relative_to(actual).as_posix(): p.read_bytes()
        for p in actual.rglob("*")
        if p.is_file() and p.name != "ConfigDumpInfo.xml"
    }
    mismatches = sorted(before.keys() ^ after.keys())
    for path in sorted(before.keys() & after.keys()):
        left, right = before[path], after[path]
        if (
            (normalized_xml(left) != normalized_xml(right))
            if path.endswith(".xml")
            else (left != right)
        ):
            mismatches.append(path)
    return {
        "equal": not mismatches,
        # Только хеш имени: чужие метаданные не попадают в протокол.
        "mismatches": [sha256(p.encode("utf-8")) for p in sorted(mismatches)],
        "generated_hashes": {
            sha256(p.encode("utf-8")): sha256(b) for p, b in sorted(before.items())
        },
        "dumped_hashes": {sha256(p.encode("utf-8")): sha256(b) for p, b in sorted(after.items())},
    }


def check_registration_kit(
    base: Path,
    kit: Path,
    platform: Path,
    *,
    user: str = "",
    run_root: Path = RUN_ROOT,
    runner: Runner = run_command,
) -> tuple[int, dict[str, object]]:
    """Копирует только 1Cv8.1CD и удаляет всю одноразовую папку в finally."""
    report: dict[str, object] = {
        "schema_version": "ed-registration-probe/1",
        "steps": [],
        "runtime_exchange_verified": False,
    }
    steps: list[dict[str, object]] = []
    report["steps"] = steps
    task_dir: Path | None = None
    try:
        base = base.resolve(strict=True)
        if not base.is_dir() or not (base / "1Cv8.1CD").is_file():
            raise ValueError("Нужна файловая база")
        manifest = RegistrationManifest.from_bytes((kit / "manifest.json").read_bytes())
        # Проверяем именно переданный комплект, а не произвольную папку расширения.
        for path, digest in manifest.file_hashes.items():
            target = (kit / path).resolve()
            if not target.is_relative_to(kit.resolve()) or sha256(target.read_bytes()) != digest:
                raise ValueError("Хеш комплекта не совпал")
        report["kit_hash"] = sha256(manifest.to_bytes())
        run_root = run_root.resolve()
        if run_root == base or run_root.is_relative_to(base):
            raise ValueError("Копия не должна находиться внутри исходной базы")
        run_root.mkdir(parents=True, exist_ok=True)
        task_dir = Path(tempfile.mkdtemp(prefix="registration-", dir=run_root)).resolve()
        if not task_dir.is_relative_to(run_root):
            raise ValueError("Копия вне рабочей папки")
        copy = task_dir / "ib"
        copy.mkdir()
        shutil.copy2(base / "1Cv8.1CD", copy / "1Cv8.1CD")
        report["base_hash"] = file_hash(copy / "1Cv8.1CD")
        if platform.is_file():
            report["platform_hash"] = file_hash(platform)
        dumped = task_dir / "dumped"
        dumped.mkdir()
        common = [str(platform), "DESIGNER", "/F", str(copy)]
        if user:
            common.extend(["/N", user])
        name = manifest.extension_name
        actions = (
            (
                "load",
                [
                    "/LoadConfigFromFiles",
                    str(kit.resolve() / "extension"),
                    "-Format",
                    "Hierarchical",
                    "-Extension",
                    name,
                ],
            ),
            ("check", ["/CheckConfig", "-Extension", name]),
            ("applicability", ["/CheckCanApplyConfigurationExtensions", "-Extension", name]),
            ("update", ["/UpdateDBCfg", "-Extension", name]),
            (
                "dump",
                ["/DumpConfigToFiles", str(dumped), "-Format", "Hierarchical", "-Extension", name],
            ),
        )
        for label, action in actions:
            result_path = task_dir / (label + ".result")
            argv = [
                *common,
                *action,
                "/DisableStartupDialogs",
                "/Out",
                str(task_dir / (label + ".log")),
                "/DumpResult",
                str(result_path),
            ]
            code = runner(argv)
            result = (
                result_path.read_text(encoding="utf-8-sig").strip()
                if result_path.is_file()
                else "missing"
            )
            steps.append({"step": label, "exit_code": code, "result_zero": result == "0"})
            if code != 0 or result != "0":
                return 1, report
        report.update(compare_extension(kit / "extension", dumped))
        config = etree.fromstring((dumped / "Configuration.xml").read_bytes())
        report["extension_version"] = config.findtext(
            f"{{{M}}}Configuration/{{{M}}}Properties/{{{M}}}Version"
        )
        report["compatibility_mode"] = manifest.compatibility_mode
        report["interface_compatibility_mode"] = manifest.interface_compatibility_mode
        return (0 if report["equal"] else 2), report
    except (
        OSError,
        ValueError,
        etree.XMLSyntaxError,
        RegistrationDeliveryError,
        subprocess.TimeoutExpired,
    ) as error:
        report["error_type"] = type(error).__name__
        return 1, report
    finally:
        # Удаляется исключительно созданный mkdtemp; исходная база недоступна для команд.
        if task_dir is not None:
            if task_dir.parent != run_root or not task_dir.name.startswith("registration-"):
                raise ValueError("Небезопасный путь удаления копии")
            shutil.rmtree(task_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--user", default="")
    args = parser.parse_args(argv)
    code, report = check_registration_kit(args.base, args.kit, args.platform, user=args.user)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
