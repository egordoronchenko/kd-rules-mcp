r"""Круг модуля менеджера через копию файловой базы КД 3, без интерфейса.

list / export / roundtrip / format-load используют внешнее соединение JScript ES3. Исходная база
не подключается; копия и протокол остаются в kdbase/run/kd3-<время>-<суффикс> для приёмки.
roundtrip --dry-run не требует базы и предсказывает потерю неизвестных процедур вне алгоритмов.
--via-writer выполняет import_manager -> render перед загрузкой; сравнение остаётся с оригиналом.
format-load получает XSD пакета через модель XDTO платформы, загружает базовую версию и расширение
и сравнивает обратную выгрузку КД 3. --base-xsd задаёт схемы, --base-from экспортирует их из копии
базы БСП. --dry-run проверяет форму входа без базы; внешние типы разрешает только платформа.
Коды format-load: 0 — без потерь, 2 — потери, 1 — ошибка; протокол — kd3-<время>-format/.

Сценарий передаётся cscript в UTF-16 LE с BOM. Его результат — UTF-8 без BOM,
ключ<TAB>значение<LF>, обратная косая и TAB/CR/LF экранированы как \\, \t, \r, \n.
Учётные данные получает только JScript из KD3_USER / KD3_PASSWORD (для базы --base-from —
KD3_FROM_USER / KD3_FROM_PASSWORD, если заданы); Python не строит строку COM.
Модели сравниваются после той же маски заголовка/идентификатора, что и текст.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from lxml import etree

from kd_rules_mcp.authoring.ed.format_package import (
    FormatPackage,
    FormatType,
    format_package_from_schema,
    load_format_package,
    render_format_package,
)
from kd_rules_mcp.console import utf8_stdout
from kd_rules_mcp.ed.canonical import canonicalize
from kd_rules_mcp.ed.diff import compare_models
from kd_rules_mcp.ed.errors import EdFormatError, EdReadError, EdResourceLimitError
from kd_rules_mcp.ed.model import EdDocument, Routine
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.schema.model import EdSchema, QName, SchemaPackage, SchemaType
from kd_rules_mcp.ed.schema.xdto import XS, metadata, read_package
from kd_rules_mcp.ed.writer import RenderMode, render
from kd_rules_mcp.ed.writer_import import import_manager
from kd_rules_mcp.ed.writer_model import decode_dto
from kd_rules_mcp.errors import Kd2Error

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "kdbase" / "run"
SCENARIO = ROOT / "kdbase" / "src" / "kd3_roundtrip.js"
TIMEOUT_S = 300
KINDS = ("pko", "pod", "pks", "pktch", "pkpd", "algorithms")
LABELS = ("ПКО", "ПОД", "ПКС", "ПКТЧ", "ПКПД", "Алгоритмы")
HEADER = re.compile(r"^([ \t]*// Менеджер обмена через универсальный формат \().*(\)[ \t]*)$")
UUID_RETURN = re.compile(
    r'^(\s*Возврат\s+)"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"(\s*;.*)$',
    re.IGNORECASE,
)


class CheckError(Exception):
    """Ошибка адаптера, которая должна попасть в протокол, а не в трассировку."""


class ProtocolParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CheckError(message)


def redact(text: str) -> str:
    """Секреты могут быть повторены платформой в тексте исключения."""
    for key in ("KD3_USER", "KD3_PASSWORD", "KD3_FROM_USER", "KD3_FROM_PASSWORD"):
        value = os.environ.get(key, "")
        if value:
            text = text.replace(value.replace('"', '""'), "<скрыто>").replace(value, "<скрыто>")
    return text


def say(text: str) -> None:
    print(redact(text).replace("\r", " ").replace("\n", " | "))


def normalize(text: str) -> str:
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def mask(text: str) -> str:
    """Маска по структуре, без номеров строк; UUID в других процедурах не меняются.

    Эталон: reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:951-969.
    """
    text = normalize(text)
    document = read_manager_text(text)
    lines = text.split("\n")
    first_routine = min((r.span.line_start for r in document.routines), default=len(lines) + 1)
    for index in range(first_routine - 1):
        if HEADER.match(lines[index]):
            lines[index] = HEADER.sub(r"\1<конвертация> от <дата>\2", lines[index])
            break
    for routine in document.routines:
        if routine.name.casefold() == "подключаемый_идентификатормодуля":
            for index in range(routine.body_span.line_start - 1, routine.body_span.line_end):
                lines[index] = UUID_RETURN.sub(r'\1"<уид>"\2', lines[index])
    return "\n".join(lines)


def predict_losses(document: EdDocument) -> list[str]:
    """Прогноз только unknown_routine; неизвестная строка ПКО не означает потерю процедуры.

    Эталон: reference/kd3-cfg/DataProcessors/ЗагрузкаМодуляМенеджера/Ext/ManagerModule.bsl:815-823;
    уточнение исполнением: docs/plans/evals/2026-10-05-kd3-roundtrip.md, шаг 5.
    """
    spans = {u.span for u in document.unknown if u.reason == "unknown_routine"}
    return [
        r.name
        for r in document.routines
        if r.span in spans and "алгоритмы" not in {name.casefold() for name in r.regions}
    ]


@dataclass(frozen=True)
class TextChange:
    kind: str
    procedure: str | None
    line_before: int
    line_after: int
    count: int
    before: tuple[str, ...]
    after: tuple[str, ...]


def _change(
    kind: str,
    procedure: str | None,
    old_line: int,
    new_line: int,
    before: list[str],
    after: list[str],
) -> TextChange:
    return TextChange(
        kind,
        procedure,
        old_line,
        new_line,
        max(len(before), len(after)),
        tuple(redact(line)[:80] for line in before),
        tuple(redact(line)[:80] for line in after),
    )


def _routine_map(document: EdDocument) -> dict[str, Routine]:
    result: dict[str, Routine] = {}
    for routine in document.routines:
        key = routine.name.casefold()
        while key in result:
            key += "#"
        result[key] = routine
    return result


def _line_opcodes(before: list[str], after: list[str]) -> list[tuple[str, int, int, int, int]]:
    """В смешанном участке удалённый вызов не подменяет соседнюю изменённую строку."""
    result = []
    for tag, i, i2, j, j2 in difflib.SequenceMatcher(
        None, before, after, autojunk=False
    ).get_opcodes():
        if tag != "replace" or i2 - i == j2 - j:
            result.append((tag, i, i2, j, j2))
            continue

        def heads(lines: list[str]) -> list[str]:
            matches = [re.match(r"\w+|//", line.lstrip()) for line in lines]
            return [match[0] if match else "" for match in matches]

        for subtag, a, a2, b, b2 in difflib.SequenceMatcher(
            None, heads(before[i:i2]), heads(after[j:j2]), autojunk=False
        ).get_opcodes():
            result.append(
                ("replace" if subtag == "equal" else subtag, i + a, i + a2, j + b, j + b2)
            )
    return result


def text_changes(before: str, after: str) -> list[TextChange]:
    """Процедуры сравниваются отдельно, чтобы соседние удаления не смешивались в один участок."""
    left, right = mask(before), mask(after)
    if left == right:
        return []
    old, new = _routine_map(read_manager_text(left)), _routine_map(read_manager_text(right))
    la, lb = left.split("\n"), right.split("\n")
    changes: list[TextChange] = []
    for key, routine in old.items():
        start = routine.span.line_start - 1
        a = la[start : routine.span.line_end]
        other = new.get(key)
        if other is None:
            changes.append(_change("потеряна процедура", routine.name, start + 1, 0, a, []))
            continue
        end_start = other.span.line_start - 1
        b = lb[end_start : other.span.line_end]
        rule = routine.name.casefold().startswith(("добавитьпко_", "добавитьпод_")) or (
            routine.name.casefold() == "заполнитьправилаконвертациипредопределенныхданных"
        )
        for tag, i, i2, j, j2 in _line_opcodes(a, b):
            if tag == "equal":
                continue
            paired = min(i2 - i, j2 - j) if tag == "replace" else 0
            for offset in range(paired):
                if a[i + offset] == b[j + offset]:
                    continue
                changes.append(
                    _change(
                        "изменена строка",
                        routine.name,
                        start + i + offset + 1,
                        end_start + j + offset + 1,
                        [a[i + offset]],
                        [b[j + offset]],
                    )
                )
            if i + paired < i2:
                changes.append(
                    _change(
                        "потеряны строки в описании правила" if rule else "прочее",
                        routine.name,
                        start + i + paired + 1,
                        end_start + j + paired + 1,
                        a[i + paired : i2],
                        [],
                    )
                )
            if j + paired < j2:
                changes.append(
                    _change(
                        "прочее",
                        routine.name,
                        start + i + paired + 1,
                        end_start + j + paired + 1,
                        [],
                        b[j + paired : j2],
                    )
                )
    for key, routine in new.items():
        if key not in old:
            changes.append(
                _change(
                    "прочее",
                    routine.name,
                    0,
                    routine.span.line_start,
                    [],
                    lb[routine.span.line_start - 1 : routine.span.line_end],
                )
            )

    # Скелет сохраняет порядок общих процедур и весь текст вне процедур, включая пустые строки.
    def skeleton(lines: list[str], routines: dict[str, Routine]) -> list[tuple[str, int]]:
        result: list[tuple[str, int]] = []
        cursor = 0
        for key, routine in routines.items():
            start = routine.span.line_start - 1
            result.extend(
                (line, index + 1) for index, line in enumerate(lines[cursor:start], cursor)
            )
            if key in old and key in new:
                result.append(("<процедура:" + key + ">", start + 1))
            cursor = routine.span.line_end
        result.extend((line, index + 1) for index, line in enumerate(lines[cursor:], cursor))
        return result

    sa, sb = skeleton(la, old), skeleton(lb, new)
    for tag, i, i2, j, j2 in difflib.SequenceMatcher(
        None, [line for line, _ in sa], [line for line, _ in sb], autojunk=False
    ).get_opcodes():
        if tag != "equal":
            changes.append(
                _change(
                    "прочее",
                    None,
                    sa[i][1] if i < len(sa) else len(la),
                    sb[j][1] if j < len(sb) else len(lb),
                    [line for line, _ in sa[i:i2]],
                    [line for line, _ in sb[j:j2]],
                )
            )
    return changes


def compare_texts(before: str, after: str, base_counts: dict[str, int]) -> dict[str, Any]:
    original = read_manager_text(before)
    returned = read_manager_text(after)
    left, right = read_manager_text(mask(before)), read_manager_text(mask(after))
    old_model = import_manager(left, project_id="kd3-check")[0]
    new_model = import_manager(right, project_id="kd3-check")[0]
    model_diff = compare_models(old_model, new_model)
    changes = text_changes(before, after)
    predicted = predict_losses(original)
    actual = [c.procedure for c in changes if c.kind == "потеряна процедура"]
    composition = {
        kind: {
            "original": original.counts[kind],
            "returned": returned.counts[kind],
            "base": base_counts[kind],
        }
        for kind in KINDS
    }
    notices = [
        f"Состав {kind}: {row['original']} / {row['returned']} / {row['base']}"
        for kind, row in composition.items()
        if len(set(row.values())) != 1
    ]
    if set(predicted) != set(actual):
        notices.append("Прогноз потерь процедур не совпал с фактом")
    return {
        "equal_after_mask": mask(before) == mask(after),
        "text_changes": [asdict(change) for change in changes],
        "reader_original": original.counts,
        "reader_returned": returned.counts,
        "composition": composition,
        "model_equal": model_diff.equal,
        "canonical_equal": canonicalize(old_model) == canonicalize(new_model),
        "model_changes": [
            {"address": c.address, "action": c.action, "fields": c.fields}
            for c in model_diff.changes
        ],
        "predicted_losses": predicted,
        "actual_losses": actual,
        "prediction_matches": set(predicted) == set(actual),
        "notices": notices,
        "status": "OK" if not changes and not notices else "РАЗЛИЧИЯ",
    }


def _unescape(value: str) -> str:
    result: list[str] = []
    index = 0
    escapes = {"\\": "\\", "t": "\t", "r": "\r", "n": "\n"}
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            if index == len(value) or value[index] not in escapes:
                raise CheckError("Неизвестное экранирование в результате сценария")
            char = escapes[value[index]]
        result.append(char)
        index += 1
    return "".join(result)


def parse_result(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("\t")
        if not separator or not key or key in result or "\t" in value:
            raise CheckError("Некорректная строка результата сценария")
        result[key] = _unescape(value)
    if result.get("protocol") != "1" or result.get("status") not in ("OK", "ERROR"):
        raise CheckError("Неполный результат сценария")
    return result


def _process_text(data: bytes) -> str:
    if data.startswith(b"\xff\xfe") or b"\0" in data:
        return data.decode("utf-16-le", errors="replace").removeprefix("\ufeff").strip()
    # Ошибки самого WSH до загрузки сценария игнорируют //U и используют OEM-кодировку.
    return data.decode("oem" if sys.platform == "win32" else "utf-8", errors="replace").strip()


def run_scenario(
    command: str, base: Path, run: Path, arguments: list[str], timeout: int
) -> dict[str, str]:
    """Без оболочки, без паролей в аргументах; результат обязан существовать даже при exit 0."""
    script, result = run / "scenario.js", run / "result.tsv"
    script.write_bytes(b"\xff\xfe" + SCENARIO.read_text(encoding="utf-8").encode("utf-16-le"))
    argv = [
        "cscript.exe",
        "//nologo",
        "//E:jscript",
        "//U",
        str(script),
        command,
        str(base),
        str(result),
        *arguments,
    ]
    try:
        process = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise CheckError(f"таймаут {timeout} с: cscript не завершил проверку") from error
    stderr = _process_text(process.stderr)
    if process.returncode or stderr:
        detail = (
            parse_result(result).get("error", stderr)
            if result.is_file()
            else stderr or _process_text(process.stdout)
        )
        raise CheckError(f"cscript: код {process.returncode}; {detail or 'нет текста ошибки'}")
    if not result.is_file():
        raise CheckError("cscript не записал файл результата")
    values = parse_result(result)
    if values["status"] != "OK":
        raise CheckError(values.get("error", "Ошибка сценария"))
    if values.get("command") != command:
        raise CheckError("Команда в результате сценария не совпала с запросом")
    return values


def copy_base(base: Path, suffix: str | None = None) -> tuple[Path, Path]:
    """Ни list, ни остальные команды не передают исходную базу COMConnector."""
    base = base.resolve()
    if not (base / "1Cv8.1CD").is_file():
        raise CheckError("нет базы КД 3: в заданной папке отсутствует 1Cv8.1CD")
    if RUNS.resolve().is_relative_to(base):
        raise CheckError("Папка запусков не может находиться внутри исходной базы")
    run = RUNS / (
        "kd3-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + (suffix or uuid4().hex[:8])
    )
    run.mkdir(parents=True)
    copied = run / "base"
    shutil.copytree(base, copied)
    return run, copied


def _number(values: dict[str, str], key: str) -> int:
    try:
        number = int(values[key])
    except (KeyError, ValueError) as error:
        raise CheckError(f"Нет счётчика {key} в результате сценария") from error
    if number < 0:
        raise CheckError(f"Отрицательный счётчик {key}")
    return number


def _counts(values: dict[str, str], index: int = 0) -> dict[str, int]:
    return {kind: _number(values, f"conversion.{index}.count.{kind}") for kind in KINDS}


def _list_output(values: dict[str, str]) -> None:
    for index in range(_number(values, "conversion_count")):
        prefix = f"conversion.{index}."
        required = [prefix + key for key in ("name", "manager_version", "format_version")]
        if any(key not in values for key in required):
            raise CheckError("Неполное описание конвертации")
        say(
            f"КОНВЕРТАЦИЯ {values[required[0]]}; менеджер {values[required[1]]}; "
            f"формат {values[required[2]]}"
        )
        for kind, count in _counts(values, index).items():
            say(f"СОСТАВ {kind} {count}")


def _write_report(path: Path, report: dict[str, Any]) -> None:
    def sanitized(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {redact(key): sanitized(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitized(item) for item in value]
        return value

    path.write_text(
        json.dumps(sanitized(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _input_schema(
    namespace: str,
    imports: tuple[str, ...],
    references: set[QName],
    own: SchemaPackage | None = None,
) -> EdSchema:
    """Контекст проверки формы, а не доказательство существования внешних типов.

    Вход не содержит схемы хозяина. Здесь регистрируются только заявленные ссылки;
    их существование и совместимость проверяет конструктор ФабрикаXDTO в живом прогоне.
    """
    external = {
        name: SchemaType(
            id=str(name),
            qname=name,
            kind="object",
            base=None,
            members=(),
            variety="atomic",
            properties=(),
            facets=(),
            open=False,
            abstract=False,
            ordered=False,
            sequenced=False,
            explicit_attributes=frozenset(),
            origin=(),
            status="complete",
        )
        for name in references
        if name.namespace in imports
    }
    packages = tuple(
        SchemaPackage(
            uri,
            None,
            None,
            (),
            (),
            tuple(typ for name, typ in external.items() if name.namespace == uri),
            "import",
            {},
        )
        for uri in imports
    )
    types = {**external, **({t.qname: t for t in own.types if t.qname} if own else {})}
    return EdSchema(
        "format-check-input",
        (*packages, *((own,) if own else ())),
        namespace,
        (),
        (),
        "input-shape",
        "unverified",
        types,
        {t.id: t for t in types.values()},
        {},
    )


def format_input(path: Path, version: str | None, base_uri: str | None) -> FormatPackage:
    """Читает JSON через FP либо готовую выгрузку; не угадывает версию для произвольного URI."""
    if path.is_dir():
        folder = path / "XDTOPackages" if (path / "XDTOPackages").is_dir() else path
        files = sorted(folder.glob("*.xml"))
        if len(files) != 1:
            raise CheckError("Папка должна содержать ровно один пакет XDTO")
        name, namespace, _, _ = metadata(files[0])
        package = read_package(folder / name / "Ext" / "Package.bin", "extension")
        if package.namespace != namespace or package.diagnostics:
            raise CheckError("Метаданные и XML пакета не совпали или прочитаны не полностью")
        imports = tuple(i.namespace for i in package.imports)
        base_uri = base_uri or next((uri for uri in imports if uri != XS), None)
        if not base_uri:
            raise CheckError("Пакет не импортирует базовый формат")
        version = version or (base_uri.rsplit("/", 1)[-1] if "/" in base_uri else None)
        if not version:
            raise CheckError("Для папки пакета задайте --base-version <версия>")
        references = {
            ref
            for typ in package.types
            for ref in (
                *((typ.base,) if typ.base else ()),
                *typ.members,
                *(p.type_ref for p in typ.properties if p.type_ref),
            )
        }
        schema = _input_schema(base_uri, imports, references, package)
        return format_package_from_schema(
            schema,
            namespace=namespace,
            metadata_name=name,
            base_namespace=base_uri,
            base_version=version,
        )
    with path.open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise CheckError("JSON пакета превышает 32 MiB")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise CheckError("Ожидался JSON-объект пакета")
    base_uri = base_uri or data.get("base_namespace")
    version = version or data.get("base_version")
    if not isinstance(base_uri, str) or not isinstance(version, str):
        raise CheckError("JSON пакета должен задавать base_namespace и base_version")
    types: tuple[FormatType, ...] = decode_dto(tuple[FormatType, ...], data.get("types", []))
    imports = (base_uri, *decode_dto(tuple[str, ...], data.get("imports", [])))
    references: set[QName] = set()

    def visit(typ: FormatType) -> None:
        references.update((*((typ.base,) if typ.base else ()), *typ.members))
        references.update(f.value_type for f in typ.facets if f.value_type)
        for prop in typ.properties:
            references.add(prop.type)
            if prop.inline_type:
                visit(prop.inline_type)

    for typ in types:
        visit(typ)
    return load_format_package(path, _input_schema(base_uri, imports, references), version)


def _xsd_root(path: Path) -> etree._Element:
    with path.open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise CheckError("XSD превышает 32 MiB")
    root = etree.fromstring(
        raw,
        etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
        ),
    )
    if (
        root.tag != f"{{{XS}}}schema"
        or not root.get("targetNamespace")
        or getattr(root.getroottree().docinfo, "doctype", "")
        or any(isinstance(e, etree._Entity) for e in root.iter())
    ):
        raise CheckError("Ожидалась XSD без DTD и сущностей с targetNamespace")
    return root


def _xsd_facts(path: Path, namespace: str) -> dict[str, Any]:
    """Сравнивает структуру платформенной XSD с XSD КД 3, без своего сериализатора.

    Типы/свойства/перечисления и границы входят в сравнение. Ключи и ТЧ — те же типы
    и связи: их распознаёт загрузчик КД 3 (reference/kd3-cfg/DataProcessors/
    ЗагрузкаСтруктурыФормата/Ext/ManagerModule.bsl:118–169,240–260).
    """
    root = _xsd_root(path)
    if root.get("targetNamespace") != namespace:
        raise CheckError("Обратная XSD имеет другое пространство имён")
    if root.find(f"{{{XS}}}include") is not None or root.find(f"{{{XS}}}redefine") is not None:
        raise CheckError("Сравнение XSD с include/redefine не поддерживается")
    facts: dict[str, Any] = {}

    def qname(node: etree._Element, value: str) -> str:
        prefix, sep, local = value.partition(":")
        uri = node.nsmap.get(prefix if sep else None)
        if not uri:
            raise CheckError("Не разрешён QName в XSD: " + value)
        return str(QName(uri, local if sep else prefix))

    def inspect(node: etree._Element, address: str) -> None:
        for restriction in (
            *node.findall(f"{{{XS}}}restriction"),
            *node.findall(f"{{{XS}}}simpleContent/{{{XS}}}extension"),
            *node.findall(f"{{{XS}}}simpleContent/{{{XS}}}restriction"),
            *node.findall(f"{{{XS}}}complexContent/{{{XS}}}extension"),
            *node.findall(f"{{{XS}}}complexContent/{{{XS}}}restriction"),
        ):
            if restriction.get("base"):
                facts[address + "/base"] = qname(restriction, restriction.get("base", ""))
            for enum in restriction.findall(f"{{{XS}}}enumeration"):
                facts[address + "/enum/" + enum.get("value", "")] = True
        # Вложенные анонимные типы обрабатываются отдельно, без двойного обхода их свойств.
        for prop in node.iter():
            if prop.tag not in (f"{{{XS}}}element", f"{{{XS}}}attribute"):
                continue
            owner = next(
                (
                    p
                    for p in prop.iterancestors()
                    if p.tag in (f"{{{XS}}}complexType", f"{{{XS}}}simpleType")
                ),
                None,
            )
            if owner is not node:
                continue
            if not prop.get("name") or prop.get("ref"):
                raise CheckError("Сравнение свойства XSD без name или с ref не поддерживается")
            key = address + "/property/" + prop.get("name", "")
            inline = prop.find(f"{{{XS}}}simpleType")
            if inline is None:
                inline = prop.find(f"{{{XS}}}complexType")
            facts[key] = {
                "type": qname(prop, prop.get("type", "")) if prop.get("type") else "inline",
                "lower": (
                    int(prop.get("minOccurs", "1"))
                    if prop.tag == f"{{{XS}}}element"
                    else int(prop.get("use", "optional") == "required")
                ),
                "upper": (
                    None
                    if prop.get("maxOccurs") == "unbounded"
                    else int(prop.get("maxOccurs", "1"))
                ),
                # nillable не сравнивается: выгрузка КД 3 ставит его всякому свойству
                # (ВыгрузкаСтруктурыФормата/Ext/ManagerModule.bsl:575), в формате он редок.
                "form": prop.get(
                    "form",
                    root.get(
                        "attributeFormDefault"
                        if prop.tag == f"{{{XS}}}attribute"
                        else "elementFormDefault",
                        "unqualified",
                    ),
                ),
                "kind": etree.QName(prop).localname,
            }
            if inline is not None:
                inspect(inline, key)

    for typ in root:
        if typ.tag not in (f"{{{XS}}}complexType", f"{{{XS}}}simpleType"):
            continue
        name = typ.get("name")
        if not name:
            raise CheckError("Безымянный тип верхнего уровня XSD")
        address = "type/" + str(QName(namespace, name))
        if address in facts:
            raise CheckError("Повтор типа в XSD: " + name)
        facts[address] = etree.QName(typ).localname
        inspect(typ, address)
    return facts


def compare_format(model: FormatPackage, expected: Path, returned: Path) -> dict[str, Any]:
    before, after = _xsd_facts(expected, model.namespace), _xsd_facts(returned, model.namespace)
    if not before:
        raise CheckError("Платформа не выгрузила типы нашего пакета")
    losses = [
        {"address": key, "expected": value, "returned": after.get(key)}
        for key, value in before.items()
        if key not in after or after[key] != value
    ]
    roles = {str(t.name): t.role for t in model.types}
    for loss in losses:
        loss["role"] = next(
            (
                role
                for name, role in roles.items()
                if loss["address"] == "type/" + name
                or loss["address"].startswith("type/" + name + "/")
            ),
            "object",
        )
    return {
        "status": "ПОТЕРИ" if losses else "OK",
        "losses": losses,
        "expected_facts": len(before),
        "returned_facts": len(after),
    }


def _format_execute(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    model = format_input(args.package, args.base_version, args.base_namespace)
    if not model.types:
        raise CheckError("Пакет не содержит типов")
    base_files = args.base_xsd or []
    for file in base_files:
        _xsd_root(file)
    if base_files and not any(
        _xsd_root(file).get("targetNamespace") == model.base_namespace for file in base_files
    ):
        raise CheckError("--base-xsd не содержит базовый URI пакета")
    if args.base_from and not (args.base_from / "1Cv8.1CD").is_file():
        raise CheckError("--base-from: отсутствует 1Cv8.1CD")
    say(
        f"ПАКЕТ {model.metadata_name}; URI {model.namespace}; база {model.base_namespace}; "
        f"версия {model.base_version}; типов {len(model.types)}"
    )
    say(
        "ПЛАН копия КД 3 → XML пакета в модель XDTO → экспорт XSD платформой → "
        "загрузка базовой версии при отсутствии → загрузка расширения → "
        "выгрузка структуры КД 3 → сравнение типов, свойств, ключей, ТЧ и перечислений"
    )
    if args.dry_run:
        say("РЕЖИМ без базы; проверена форма входа, внешние типы и потери не проверены")
        return "OK", {"dry_run": True, "base_checked": False, "status": "OK"}
    if not base_files and not args.base_from:
        raise CheckError("format-load требует --base-xsd <файлы> или --base-from <база БСП>")
    if args.timeout <= 0:
        raise CheckError("Таймаут должен быть положительным")
    base_value = args.base or os.environ.get("KD3_BASE")
    if not base_value:
        raise CheckError("Задайте --base <папка файловой базы КД 3> или KD3_BASE")
    run, copied = copy_base(Path(base_value), "format")
    say(f"ПРОТОКОЛ {run}")
    copies = [copied]
    try:
        if args.base_from:
            source = args.base_from.resolve()
            if run.resolve().is_relative_to(source):
                raise CheckError("Папка запусков внутри исходной базы БСП")
            bsp_copy = run / "bsp-base"
            copies.append(bsp_copy)
            shutil.copytree(source, bsp_copy)
            stage = run / "base-export"
            stage.mkdir()
            values = run_scenario(
                "base-export",
                bsp_copy,
                stage,
                [str(stage), model.base_namespace, *model.imports],
                args.timeout,
            )
            count = _number(values, "schema_count")
            if not count or any(f"schema.{i}.path" not in values for i in range(count)):
                raise CheckError("Неполный список экспортированных XSD")
            base_files = [Path(values[f"schema.{i}.path"]) for i in range(count)]
        else:
            stage = run / "base-xsd"
            stage.mkdir()
            # Имена сохраняются: schemaLocation может ссылаться на соседний файл.
            if len({p.name.casefold() for p in base_files}) != len(base_files):
                raise CheckError("Повтор имени файла --base-xsd")
            for file in base_files:
                shutil.copy2(file, stage / file.name)
            base_files = [stage / file.name for file in base_files]
        for file in base_files:
            if not file.resolve().is_relative_to(run.resolve()):
                raise CheckError("Сценарий вернул путь XSD вне папки прогона")
            _xsd_root(file)
        if not any(
            _xsd_root(file).get("targetNamespace") == model.base_namespace for file in base_files
        ):
            raise CheckError("Экспорт XSD не содержит базовый URI пакета")
        if args.package.is_dir():
            source_folder = args.package / "XDTOPackages"
            if not source_folder.is_dir():
                source_folder = args.package
            # Готовый пакет испытывается как есть, без повторной сериализации FP.
            files = {
                f"XDTOPackages/{model.metadata_name}.xml": (
                    source_folder / f"{model.metadata_name}.xml"
                ).read_bytes(),
                f"XDTOPackages/{model.metadata_name}/Ext/Package.bin": (
                    source_folder / model.metadata_name / "Ext" / "Package.bin"
                ).read_bytes(),
            }
        else:
            files = render_format_package(model)
        for name, content in files.items():
            target = run / "package" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        binary = run / "package" / "XDTOPackages" / model.metadata_name / "Ext" / "Package.bin"
        values = run_scenario(
            "format-load",
            copied,
            run,
            [
                str(binary),
                model.namespace,
                model.base_namespace,
                model.base_version,
                str(run),
                *(str(file) for file in base_files),
            ],
            args.timeout,
        )
        if values.get("extension_loaded") != "true":
            raise CheckError("Нет подтверждения загрузки расширения КД 3")
        report = compare_format(model, run / "extension.xsd", run / "returned.xsd")
        report.update(
            {
                "dry_run": False,
                "base_checked": True,
                "run": str(run),
                "namespace": model.namespace,
                "base_namespace": model.base_namespace,
                "base_version": model.base_version,
                "base_loaded": values.get("base_loaded"),
            }
        )
        for loss in report["losses"]:
            say(
                f"ПОТЕРЯ {loss['role']} {loss['address']}; "
                f"ожидалось {loss['expected']}; вернулось {loss['returned']}"
            )
        _write_report(run / "report.json", report)
        return report["status"], report
    except (CheckError, OSError, ValueError, Kd2Error, etree.Error) as error:
        _write_report(run / "report.json", {"status": "ОШИБКА", "error": str(error)})
        raise
    finally:
        if not args.keep_base:
            for copy in copies:
                shutil.rmtree(copy, ignore_errors=True)


def _parser() -> ProtocolParser:
    parser = ProtocolParser(description="Круг модуля менеджера через копию базы КД 3")
    parser.add_argument("--base", type=Path, help="папка файловой базы или KD3_BASE")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_S, help="таймаут cscript, секунды")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "export", "roundtrip", "format-load"):
        child = commands.add_parser(command)
        child.add_argument("--base", type=Path, default=argparse.SUPPRESS)
        child.add_argument("--timeout", type=int, default=argparse.SUPPRESS)
        child.add_argument(
            "--keep-base",
            action="store_true",
            help="не удалять копию базы после запуска (по умолчанию остаются тексты и отчёт)",
        )
        if command == "export":
            child.add_argument("conversion")
            child.add_argument("output", type=Path)
        elif command == "roundtrip":
            child.add_argument("module", type=Path)
            child.add_argument(
                "--like", help="конвертация-образец релиза; обязательна для живого круга"
            )
            child.add_argument("--via-writer", choices=("preserve", "canonical"))
            child.add_argument("--report", type=Path)
            child.add_argument("--dry-run", action="store_true")
        elif command == "format-load":
            child.add_argument("package", type=Path, help="JSON пакета или папка XDTOPackages")
            sources = child.add_mutually_exclusive_group()
            sources.add_argument("--base-xsd", nargs="+", type=Path)
            sources.add_argument("--base-from", type=Path, help="файловая база БСП; только копия")
            child.add_argument(
                "--base-version", help="версия для папки пакета; JSON уже содержит её"
            )
            child.add_argument(
                "--base-namespace", help="основной URI для папки с несколькими импортами"
            )
            child.add_argument("--report", type=Path)
            child.add_argument("--dry-run", action="store_true")
    return parser


def _execute(args: argparse.Namespace) -> tuple[str, dict[str, Any] | None]:
    if args.command == "format-load":
        return _format_execute(args)
    original = ""
    original_bytes = b""
    submitted = b""
    if args.command == "roundtrip":
        original_bytes = args.module.read_bytes()
        original = original_bytes.decode("utf-8-sig")
        document = read_manager_text(original)
        submitted = original_bytes
        if args.via_writer:
            mode: RenderMode = args.via_writer
            model = import_manager(
                read_manager_text(original_bytes.decode("utf-8")), project_id="kd3-check"
            )[0]
            submitted = render(model, mode).data
            say(f"ПИСАТЕЛЬ {mode}")
        predicted = predict_losses(document)
        say("ПРОГНОЗ потерянные процедуры: " + (", ".join(predicted) or "нет"))
        if args.dry_run:
            say("РЕЖИМ прогноз без базы; текст и состав после КД 3 не проверены")
            status = "РАЗЛИЧИЯ" if predicted else "OK"
            return status, {
                "dry_run": True,
                "predicted_losses": predicted,
                "reader_original": document.counts,
                "via_writer": args.via_writer,
                "base_checked": False,
                "status": status,
            }
        if not args.like:
            raise CheckError("roundtrip требует --like <конвертация-образец>")
    if args.timeout <= 0:
        raise CheckError("Таймаут должен быть положительным")
    base_value = args.base or os.environ.get("KD3_BASE")
    if not base_value:
        raise CheckError("Задайте --base <папка файловой базы КД 3> или KD3_BASE")
    run, copied = copy_base(Path(base_value))
    say(f"КОПИЯ БАЗЫ {copied}")
    arguments: list[str] = []
    output = run / "returned.bsl"
    if args.command == "export":
        arguments = [args.conversion, str(output)]
    elif args.command == "roundtrip":
        upload = run / "upload.bsl"
        upload.write_bytes(submitted)
        (run / "original.bsl").write_bytes(original_bytes)
        name = "Круг " + uuid4().hex
        arguments = [str(upload), name, args.like, str(output)]
    try:
        values = run_scenario(args.command, copied, run, arguments, args.timeout)
    finally:
        # Копия базы — сотни мегабайт на запуск; для разбора результата хватает текстов и отчёта.
        if not args.keep_base:
            shutil.rmtree(copied, ignore_errors=True)
    _list_output(values)
    if args.command == "list":
        return "OK", None
    if not output.is_file():
        raise CheckError("КД 3 не записала модуль результата")
    if _number(values, "conversion_count") != 1:
        raise CheckError("Ожидалась одна конвертация в результате")
    if args.command == "export":
        shutil.copy2(output, args.output)
        say(f"МОДУЛЬ {args.output}")
        return "OK", None
    report = compare_texts(original, output.read_bytes().decode("utf-8-sig"), _counts(values))
    report.update(
        {
            "dry_run": False,
            "base_checked": True,
            "via_writer": args.via_writer,
            "run": str(run),
            "conversion": values["conversion.0.name"],
        }
    )
    say("СОСТАВ вид | исходный текст | вернувшийся текст | база")
    for kind, label in zip(KINDS, LABELS, strict=True):
        row = report["composition"][kind]
        say(f"СОСТАВ {label} | {row['original']} | {row['returned']} | {row['base']}")
    for change in report["text_changes"]:
        say(
            f"РАЗЛИЧИЕ {change['kind']}; {change['procedure'] or 'вне процедур'}; "
            f"строка {change['line_before']}; строк {change['count']}"
        )
    for change in report["model_changes"]:
        say(f"МОДЕЛЬ {change['action']} {change['address']}")
    for notice in report["notices"]:
        say("ЗАМЕЧАНИЕ " + notice)
    _write_report(run / "report.json", report)
    return report["status"], report


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    try:
        args = _parser().parse_args(argv)
        status, report = _execute(args)
        if report is not None and args.report:
            _write_report(args.report, report)
            say(f"ОТЧЁТ {args.report}")
    except (
        CheckError,
        OSError,
        ValueError,
        EdFormatError,
        EdReadError,
        EdResourceLimitError,
        Kd2Error,
        etree.Error,
    ) as error:
        say("ОШИБКА " + str(error))
        status = "ОШИБКА"
    say("ИТОГ " + status)
    say("КОНЕЦ")
    return 0 if status == "OK" else 2 if status == "ПОТЕРИ" else 1


if __name__ == "__main__":
    sys.exit(main())
