"""Круг носителя пакета XDTO на одноразовой копии файловой базы.

uv run python kdbase/ed_format_package_check.py --base <папка> --extension <папка>
--platform <1cv8.exe> [--user <пользователь>]. Серверные базы не поддерживаются.
Второй этап: --version <ключ> --other-version <ключ> --namespace <URI>
--object-type <имя>. Вместо --extension можно передать --host-dump <выгрузка>
и --plan <план>: сценарий сам строит временный носитель с вымышленным объектом.
0 — круг совпал, 1 — ошибка команды/среды, 2 — структурное расхождение.
В протоколе нет содержимого базы, параметров запуска и журналов платформы.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from kd2_rules_mcp.authoring.ed.format_package import (
    FORMAT_OVERRIDE_MODULE,
    FormatDeclaration,
    FormatPackage,
    FormatProperty,
    FormatType,
    read_format_host,
    render_format_extension,
)
from kd2_rules_mcp.authoring.ed.manifest import json_bytes, sha256
from kd2_rules_mcp.authoring.ed.xml_dump import read_description
from kd2_rules_mcp.ed.routes import read_routes
from kd2_rules_mcp.ed.schema import QName, load_schema
from kd2_rules_mcp.ed.schema.xdto import XS, XSI, parse_qname
from kd2_rules_mcp.errors import Kd2Error

Runner = Callable[[Sequence[str]], int]
RUN_ROOT = Path(__file__).resolve().parent / "run"


@dataclass(frozen=True, slots=True)
class DeclarationProbe:
    namespace: str
    version: str
    object_type: str
    other_version: str

    def __post_init__(self) -> None:
        if not all((self.namespace, self.version, self.object_type, self.other_version)):
            raise ValueError("Для пробы нужны URI, тип и две версии")
        if self.version == self.other_version:
            raise ValueError("Контрольная версия должна отличаться буква в букву")


def render_declaration_script(
    command: str, base: Path, result: Path, extension_name: str, user: str, probe: DeclarationProbe
) -> str:
    """JScript ES3, как kd3_roundtrip.js; без запроса и чтения прикладных данных.

    Вход исполнителя: CommonModules/ОбменДаннымиXDTOСервер/Ext/Module.bsl,
    ДоступныеРасширенияФормата — экспортный, модуль ExternalConnection=true.
    Безопасный режим снимается до нового сеанса (пилот маршрута, шаг 3.4).
    """
    if command not in ("activate", "inspect"):
        raise ValueError("Неизвестный этап пробы")
    values = [command, str(base), str(result), extension_name, user]
    declarations = "\n".join(
        "var " + key + " = " + json.dumps(value, ensure_ascii=True) + ";"
        for key, value in zip(
            ("command", "basePath", "resultPath", "extensionName", "user"), values, strict=True
        )
    )
    declarations += "\n" + "\n".join(
        "var " + key + " = " + json.dumps(value, ensure_ascii=True) + ";"
        for key, value in (
            ("uri", probe.namespace),
            ("version", probe.version),
            ("objectType", probe.object_type),
            ("otherVersion", probe.other_version),
        )
    )
    return (
        declarations
        + r"""
function quoteConnection(value) { return '"' + value.replace(/"/g, '""') + '"'; }
function present(value) { return value !== undefined && value !== null; }
function writeResult(text) {
    var stream = new ActiveXObject("ADODB.Stream");
    stream.Type = 2;
    stream.Charset = "utf-8";
    stream.Open();
    stream.WriteText(text);
    stream.SaveToFile(resultPath, 2);
    stream.Close();
}
var exitCode = 0;
try {
    var connection = "File=" + quoteConnection(basePath) + ";";
    if (user !== "") connection += "Usr=" + quoteConnection(user) + ";";
    var connector = new ActiveXObject("V83.COMConnector");
    var base = connector.Connect(connection);
    if (command === "activate") {
        var found = null;
        var extensions = new Enumerator(base.РасширенияКонфигурации.Получить());
        for (; !extensions.atEnd(); extensions.moveNext()) {
            var extension = extensions.item();
            if (String(extension.Имя) === extensionName) {
                if (found !== null) throw new Error("ambiguous");
                found = extension;
            }
        }
        if (found === null) throw new Error("absent");
        found.ЗащитаОтОпасныхДействий.ПредупреждатьОбОпасныхДействиях = false;
        found.БезопасныйРежим = false;
        found.Записать();
        writeResult('{"safe_mode_disabled":' + (!found.БезопасныйРежим) + '}');
    } else {
        var available = base.ОбменДаннымиXDTOСервер.ДоступныеРасширенияФормата(version);
        var other = base.ОбменДаннымиXDTOСервер.ДоступныеРасширенияФормата(otherVersion);
        var typ = base.ФабрикаXDTO.Тип(uri, objectType);
        writeResult('{"uri_available":' + present(available.Получить(uri)) +
            ',"type_found":' + present(typ) +
            ',"other_version_absent":' + (!present(other.Получить(uri))) + '}');
    }
} catch (error) {
    // Текст COM-ошибки может содержать данные базы: наружу только булев результат.
    writeResult('{"ok":false}');
    exitCode = 1;
}
WScript.Quit(exitCode);
"""
    )


def inspect_declaration(
    copy: Path,
    task_dir: Path,
    extension_name: str,
    user: str,
    probe: DeclarationProbe,
    runner: Runner,
) -> tuple[bool, dict[str, object]]:
    """Два процесса cscript гарантируют новый сеанс после записи свойств расширения."""
    report: dict[str, object] = {
        "extension": extension_name,
        "namespace": probe.namespace,
        "version": probe.version,
        "object_type": probe.object_type,
        "other_version": probe.other_version,
    }
    for command, expected in (
        ("activate", {"safe_mode_disabled"}),
        ("inspect", {"uri_available", "type_found", "other_version_absent"}),
    ):
        script, result = task_dir / (command + ".js"), task_dir / (command + ".json")
        script.write_bytes(
            b"\xff\xfe"
            + render_declaration_script(command, copy, result, extension_name, user, probe).encode(
                "utf-16-le"
            )
        )
        code = runner(["cscript.exe", "//nologo", "//E:jscript", "//U", str(script)])
        values = json.loads(result.read_text("utf-8-sig")) if result.is_file() else {}
        # Принимаем лишь известные булевы поля, не произвольный текст COM/подставного запуска.
        valid = isinstance(values, dict) and set(values) == expected
        valid = valid and all(type(v) is bool for v in values.values())
        report[command + "_ok"] = code == 0 and valid and all(values.values())
        if valid:
            report.update(values)
        if not report[command + "_ok"]:
            return False, report
    return True, report


def file_hash(path: Path) -> str:
    """Хеш больших файлов базы без загрузки целиком в память."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_command(argv: Sequence[str]) -> int:
    """Как в ed_registration_kit_check: без оболочки, видимых окон и печати журналов."""
    return subprocess.run(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=600,
        check=False,
    ).returncode


def normalized_package_xml(content: bytes, *, resolve_qnames: bool = True) -> bytes:
    """Сохраняет порядок типов/свойств, атрибуты и текст; QName сравнивает по URI.

    Префиксы, отступы, декларация XML и порядок атрибутов — оформление.
    Отсутствующий атрибут и явно заданное умолчание остаются различными.
    """
    root = etree.fromstring(
        content,
        etree.XMLParser(
            remove_blank_text=True, resolve_entities=False, no_network=True, load_dtd=False
        ),
    )
    if getattr(root.getroottree().docinfo, "doctype", "") or any(
        isinstance(n, etree._Entity) for n in root.iter()
    ):
        raise ValueError("DTD запрещён")

    def shape(element: etree._Element) -> object:
        attributes = []
        for raw_key, raw_value in element.attrib.items():
            key, value = str(raw_key), str(raw_value)
            if resolve_qnames and key in ("type", "base", f"{{{XSI}}}type"):
                value = str(parse_qname(element, value))
            elif resolve_qnames and key == "memberTypes":
                value = " ".join(str(parse_qname(element, s)) for s in value.split())
            attributes.append((key, value))
        return [
            element.tag,
            sorted(attributes),
            element.text or "",
            [shape(c) for c in element if isinstance(c.tag, str)],
        ]

    return json_bytes(shape(root))


def compare_format_extension(expected: Path, actual: Path) -> dict[str, object]:
    """Добавленный/потерянный файл тоже расхождение; Package.bin — XML, не бинарный блок."""

    def files(root: Path) -> dict[str, bytes]:
        return {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*")
            if p.is_file() and p.name != "ConfigDumpInfo.xml"
        }

    before, after = files(expected), files(actual)
    mismatches = sorted(before.keys() ^ after.keys())
    for path in sorted(before.keys() & after.keys()):
        if path.endswith(".bsl"):
            if before[path].decode("utf-8-sig").replace("\r\n", "\n") != after[path].decode(
                "utf-8-sig"
            ).replace("\r\n", "\n"):
                mismatches.append(path)
            continue
        resolve = path.endswith("/Package.bin")
        if normalized_package_xml(before[path], resolve_qnames=resolve) != normalized_package_xml(
            after[path], resolve_qnames=resolve
        ):
            mismatches.append(path)
    return {
        "equal": not mismatches,
        "compared_files": len(before.keys() & after.keys()),
        "mismatch_count": len(mismatches),
        "mismatches": [sha256(p.encode()) for p in sorted(mismatches)],
        "generated_hashes": {sha256(p.encode()): sha256(b) for p, b in sorted(before.items())},
        "dumped_hashes": {sha256(p.encode()): sha256(b) for p, b in sorted(after.items())},
    }


def check_format_package(
    base: Path,
    extension: Path,
    platform: Path,
    *,
    user: str = "",
    run_root: Path = RUN_ROOT,
    runner: Runner = run_command,
    declaration_probe: DeclarationProbe | None = None,
) -> tuple[int, dict[str, object]]:
    """Копирует только 1Cv8.1CD, проверяет каждый /DumpResult, удаляет только свою копию."""
    steps: list[dict[str, object]] = []
    report: dict[str, object] = {
        "schema_version": "ed-format-package-probe/1",
        "steps": steps,
        "runtime_exchange_verified": False,
    }
    if declaration_probe is not None:
        report["runtime_declaration_verified"] = False
    task_dir: Path | None = None
    try:
        base = base.resolve(strict=True)
        extension = extension.resolve(strict=True)
        if not base.is_dir() or not (base / "1Cv8.1CD").is_file():
            raise ValueError("Нужна файловая база")
        config = read_description(
            "Configuration.xml",
            (extension / "Configuration.xml").read_text("utf-8-sig"),
            "Configuration",
        )
        name = config.name
        if config.props.get("ConfigurationExtensionPurpose") != "Customization":
            raise ValueError("Нужно расширение конфигурации")
        report["compatibility_mode"] = config.props.get("ConfigurationExtensionCompatibilityMode")
        report["interface_compatibility_mode"] = config.props.get("InterfaceCompatibilityMode")
        run_root = run_root.resolve()
        if (
            run_root == base
            or run_root.is_relative_to(base)
            or extension.is_relative_to(run_root)
            or run_root.is_relative_to(extension)
        ):
            raise ValueError("Рабочая папка пересекается с исходными данными")
        run_root.mkdir(parents=True, exist_ok=True)
        task_dir = Path(tempfile.mkdtemp(prefix="format-package-", dir=run_root)).resolve()
        if task_dir.parent != run_root:
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
        actions = (
            (
                "load",
                [
                    "/LoadConfigFromFiles",
                    str(extension),
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
            code = runner(
                [
                    *common,
                    *action,
                    "/DisableStartupDialogs",
                    "/Out",
                    str(task_dir / (label + ".log")),
                    "/DumpResult",
                    str(result_path),
                ]
            )
            result = result_path.read_text("utf-8-sig").strip() if result_path.is_file() else ""
            steps.append({"step": label, "exit_code": code, "result_zero": result == "0"})
            if code != 0 or result != "0":
                return 1, report
        report.update(compare_format_extension(extension, dumped))
        if declaration_probe is not None:
            ok, runtime = inspect_declaration(copy, task_dir, name, user, declaration_probe, runner)
            report["declaration"] = runtime
            report["runtime_declaration_verified"] = ok
            if not ok:
                return 1, report
        return (0 if report["equal"] else 2), report
    except (
        OSError,
        ValueError,
        Kd2Error,
        etree.XMLSyntaxError,
        subprocess.TimeoutExpired,
    ) as error:
        report["error_type"] = type(error).__name__
        return 1, report
    finally:
        if task_dir is not None:
            if task_dir.parent != run_root or not task_dir.name.startswith("format-package-"):
                raise ValueError("Небезопасный путь удаления копии")
            shutil.rmtree(task_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--extension", type=Path)
    source.add_argument("--host-dump", type=Path)
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--user", default="")
    parser.add_argument("--version")
    parser.add_argument("--other-version")
    parser.add_argument("--namespace", default="urn:kd2:format-declaration-probe")
    parser.add_argument("--object-type", default="ProbeObject")
    parser.add_argument("--plan")
    args = parser.parse_args(argv)
    generated: Path | None = None
    probe: DeclarationProbe | None = None
    try:
        if args.version is not None:
            probe = DeclarationProbe(
                args.namespace,
                args.version,
                args.object_type,
                args.other_version or ("1.3" if args.version != "1.3" else "1.4"),
            )
        if args.host_dump is not None:
            if probe is None:
                raise ValueError("С --host-dump нужна точная --version")
            # Сначала статическая сборка; содержимое базы для модели не читается.
            payload = build_declaration_probe(args.host_dump, probe, plan_name=args.plan)
            RUN_ROOT.mkdir(parents=True, exist_ok=True)
            generated = Path(tempfile.mkdtemp(prefix="format-declare-input-", dir=RUN_ROOT))
            extension = generated / "extension"
            for path, content in payload.items():
                target = extension / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        else:
            extension = args.extension
        code, report = check_format_package(
            args.base,
            extension,
            args.platform,
            user=args.user,
            declaration_probe=probe,
            run_root=generated / "run" if generated is not None else RUN_ROOT,
        )
    except (OSError, ValueError, Kd2Error) as error:
        code, report = 1, {"error_type": type(error).__name__}
    finally:
        if generated is not None:
            if generated.resolve().parent != RUN_ROOT.resolve() or not generated.name.startswith(
                "format-declare-input-"
            ):
                raise ValueError("Небезопасный путь удаления порождённых файлов")
            shutil.rmtree(generated)
    if args.version is not None:
        # Режим объявления печатает только имена, URI, версии и булевы факты.
        visible = {
            key: value
            for key, value in report.items()
            if key
            in (
                "compatibility_mode",
                "interface_compatibility_mode",
                "error_type",
                "runtime_declaration_verified",
                "runtime_exchange_verified",
                "equal",
            )
        }
        visible.update(
            {
                "namespace": args.namespace,
                "version": args.version,
                "object_type": args.object_type,
                "other_version": probe.other_version if probe else args.other_version,
                "ok": code == 0,
                "steps": [
                    {
                        "step": step["step"],
                        "ok": step["exit_code"] == 0 and step["result_zero"] is True,
                    }
                    for step in steps
                    if isinstance(step, dict)
                ]
                if isinstance((steps := report.get("steps")), list)
                else [],
            }
        )
        if isinstance((runtime := report.get("declaration")), dict):
            visible["declaration"] = runtime
        visible.setdefault("runtime_declaration_verified", False)
        report = visible
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return code


def build_declaration_probe(
    root: Path, probe: DeclarationProbe, *, plan_name: str | None = None
) -> dict[str, bytes]:
    """Минимальный вымышленный объект; исходники хозяина только читаются, в git не копируются."""
    routes = read_routes(root)
    base_namespace = "http://v8.1c.ru/edi/edi_stnd/EnterpriseData/" + probe.version
    packages = [p for p in routes.packages if p.namespace == base_namespace]
    if len(packages) != 1 or packages[0].package_path is None:
        raise ValueError("Нет однозначного пакета точной версии в выгрузке хозяина")
    imports = {
        p.namespace: root / p.package_path for p in routes.packages if p.package_path is not None
    }
    schema = load_schema(root / packages[0].package_path, locate_import=imports.get)
    config = read_description(
        "Configuration.xml", (root / "Configuration.xml").read_text("utf-8-sig"), "Configuration"
    )
    language = config.props["DefaultLanguage"].removeprefix("Language.")
    paths = [
        "Configuration.xml",
        "Languages/" + language + ".xml",
        "CommonModules/" + FORMAT_OVERRIDE_MODULE + ".xml",
        "CommonModules/" + FORMAT_OVERRIDE_MODULE + "/Ext/Module.bsl",
    ]
    if plan_name is not None:
        paths += [
            "ExchangePlans/" + plan_name + ".xml",
            "ExchangePlans/" + plan_name + "/Ext/ManagerModule.bsl",
        ]
    descriptions = {path: (root / path).read_text("utf-8-sig") for path in paths}
    own = QName(probe.namespace, probe.object_type)
    key = QName(probe.namespace, "ProbeKey")
    model = FormatPackage(
        "fmt_ProbePackage",
        probe.namespace,
        probe.version,
        base_namespace,
        (
            FormatType(
                key, "key", (FormatProperty(QName(probe.namespace, "Id"), QName(XS, "string")),)
            ),
            FormatType(
                own,
                "object",
                (FormatProperty(QName(probe.namespace, "Key"), key),),
                exported=True,
                key_property=QName(probe.namespace, "Key"),
            ),
        ),
        schema,
    )
    return render_format_extension(
        model,
        read_format_host(descriptions),
        extension_name="fmt_DeclarationProbe",
        prefix="fmt_",
        declaration=FormatDeclaration(probe.version, descriptions, routes, plan_name),
    )


if __name__ == "__main__":
    raise SystemExit(main())
