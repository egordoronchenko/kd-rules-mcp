"""Вливание файлов комплекта в выгрузку без пересериализации чужого XML.

Эталоны и строки: docs/plans/ed-extension-delivery-2026-10.md.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from lxml import etree

from kd_rules_mcp.authoring.ed.manifest import sha256
from kd_rules_mcp.authoring.ed.xml_dump import XR, M
from kd_rules_mcp.ed.lexer import tokenize
from kd_rules_mcp.errors import RegistrationToolError

_TAGS = re.compile(
    r'<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[^>]*\?>|</?[\w:.-]+(?:[^>"\']|"[^"]*"|\'[^\']*\')*>'
)


def refuse(message: str, code: str = "delivery_conflict") -> NoReturn:
    raise RegistrationToolError(message, code=code)


def xml(raw: bytes) -> etree._Element:
    try:
        return etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as error:
        raise RegistrationToolError(
            "Выгрузка расширения не читается", code="delivery_invalid_extension"
        ) from error


def prop(root: etree._Element, name: str) -> str:
    return root.findtext(f"*/{{{M}}}Properties/{{{M}}}{name}", "")


class XmlInsert:
    """Вставки по границам элементов: прежние байты остаются на месте."""

    def __init__(self, raw: bytes):
        self.text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        self.root = xml(raw)
        elements = iter(e for e in self.root.iter() if isinstance(e.tag, str))
        stack = []
        self.spans: dict[etree._Element, tuple[int, int, int]] = {}
        for token in _TAGS.finditer(self.text):
            value = token.group()
            if value.startswith(("<!", "<?")):
                continue
            if value.startswith("</"):
                element, start, opened = stack.pop()
                self.spans[element] = (start, opened, token.start())
            else:
                element = next(elements)
                if value.endswith("/>"):
                    self.spans[element] = (token.start(), token.end(), token.start())
                else:
                    stack.append((element, token.start(), token.end()))
        self.edits: list[tuple[int, int, str]] = []

    def add(
        self,
        parent: etree._Element,
        items: list[etree._Element],
        before: etree._Element | None = None,
    ) -> None:
        if not items:
            return
        start, opened, closed = self.spans[parent]
        indent = re.search(r"(?:^|\n)([ \t]*)[^\n]*$", self.text[:start])
        padding = indent.group(1) if indent else ""
        child_padding = padding + "\t"
        if len(parent):
            child_start = self.spans[parent[0]][0]
            match = re.search(r"\n([ \t]*)$", self.text[:child_start])
            if match:
                child_padding = match.group(1)
        wrapper = etree.Element(parent.tag, nsmap=cast(Any, parent.nsmap))
        for item in items:
            copy = deepcopy(item)
            copy.tail = None
            wrapper.append(copy)
        etree.indent(wrapper, space="\t")
        rendered = etree.tostring(wrapper, encoding="unicode")
        inner = rendered[rendered.index(">") + 1 : rendered.rfind("</")].strip("\n")
        # Первая табуляция относится к временному контейнеру, остальные — к вложенности.
        inner = "\n".join(child_padding + line.removeprefix("\t") for line in inner.splitlines())
        if closed == start:
            opening = self.text[start:opened].removesuffix("/>") + ">"
            tag = re.match(r"<([\w:.-]+)", opening)
            assert tag is not None
            self.edits.append(
                (start, opened, opening + "\n" + inner + "\n" + padding + "</" + tag.group(1) + ">")
            )
        else:
            point = self.spans[before][0] if before is not None else closed
            line_start = self.text.rfind("\n", 0, point) + 1
            if self.text[line_start:point].strip():
                self.edits.append(
                    (
                        point,
                        point,
                        "\n" + inner + "\n" + (child_padding if before is not None else padding),
                    )
                )
            else:
                self.edits.append((line_start, line_start, inner + "\n"))

    def result(self) -> bytes:
        text = self.text
        for _, (start, end, value) in sorted(
            enumerate(self.edits), key=lambda item: (item[1][0], item[0]), reverse=True
        ):
            text = text[:start] + value + text[end:]
        raw = text.encode("utf-8")
        xml(raw)
        return raw


@dataclass(frozen=True)
class MergeResult:
    files: dict[str, bytes]
    before: dict[str, bytes | None]
    summaries: dict[str, list[str]]
    inputs: dict[str, str | None]

    def rows(self) -> list[dict]:
        return [
            {
                "path": name,
                "action": "create"
                if self.before[name] is None
                else "change"
                if self.before[name] != content
                else "unchanged",
                "size": len(content),
                "sha256_before": sha256(previous)
                if (previous := self.before[name]) is not None
                else None,
                "sha256_after": sha256(content),
                "changes": self.summaries[name],
            }
            for name, content in sorted(self.files.items())
        ]


def _version(value: str) -> tuple[int, ...]:
    if value == "DontUse":
        return (999,)
    if not re.fullmatch(r"Version\d+_\d+_\d+", value):
        refuse("Неизвестный режим совместимости расширения", "delivery_compatibility")
    return tuple(map(int, value.removeprefix("Version").split("_")))


def _route_merge(old: bytes | None, new: bytes, owned: bytes | None) -> bytes:
    text = (old or b"").decode("utf-8-sig").replace("\r\n", "\n")
    addition = new.decode("utf-8-sig").replace("\r\n", "\n")
    tokens = [t for t in tokenize(addition) if t.kind not in ("comment", "directive")]
    keys = {
        t.value
        for i, t in enumerate(tokens)
        if t.kind == "string"
        and i >= 2
        and tokens[i - 1].value == "("
        and tokens[i - 2].folded in ("вставить", "insert")
    }
    old_tokens = [t for t in tokenize(text) if t.kind not in ("comment", "directive")]
    # Чужая запись того же ключа запрещена даже при совпадении имени модуля.
    marker = "// kd-rules-mcp:route:" + sha256(new)[:20]
    block = marker + "\n" + addition.rstrip("\n") + "\n// kd-rules-mcp:end-route\n"
    names = {t.folded for i, t in enumerate(tokens) if i and tokens[i - 1].folded == "процедура"}
    if owned is not None:
        previous = owned.decode("utf-8-sig").replace("\r\n", "\n")
        for match in re.finditer(
            r"// kd-rules-mcp:route:[a-f0-9]{20}\n[\s\S]*?// kd-rules-mcp:end-route\n", previous
        ):
            previous_tokens = tokenize(match.group())
            if not any(
                t.folded in names
                for i, t in enumerate(previous_tokens)
                if i and previous_tokens[i - 1].folded == "процедура"
            ):
                continue
            if text.count(match.group()) != 1:
                refuse("Маршрут изменён вне предыдущей доставки")
            text = text.replace(match.group(), "", 1)
        old_tokens = [t for t in tokenize(text) if t.kind not in ("comment", "directive")]
    for i, token in enumerate(old_tokens):
        if (
            token.kind == "string"
            and token.value in keys
            and i >= 2
            and old_tokens[i - 2].folded in ("вставить", "insert")
        ):
            refuse("В модуле менеджера плана есть чужой маршрут той же версии")
    if any(
        t.folded in names
        for i, t in enumerate(old_tokens)
        if i and old_tokens[i - 1].folded in ("процедура", "функция")
    ):
        refuse("Имя процедуры маршрута занято")
    return (text + ("\n" if text and not text.endswith("\n\n") else "") + block).encode("utf-8")


def merge_extension(
    root: Path, base: Path, generated: dict[str, bytes], owned: dict[str, bytes] | None = None
) -> MergeResult:
    """Предварительная сборка: никаких записей на диск."""
    owned = owned or {}
    inputs: dict[str, str | None] = {}

    def read(name: str) -> bytes | None:
        path = root / name
        if (
            path.is_symlink()
            or path.is_junction()
            or any(p.is_symlink() or p.is_junction() for p in path.parents)
        ):
            refuse("Ссылка внутри выгрузки запрещена", "delivery_invalid_extension")
        value = path.read_bytes() if path.is_file() else None
        inputs[name] = sha256(value) if value is not None else None
        return value

    configuration = read("Configuration.xml")
    if configuration is None:
        refuse("Нет Configuration.xml расширения", "delivery_invalid_extension")
    config = xml(configuration)
    if prop(config, "ObjectBelonging") != "Adopted":
        refuse("Configuration.xml не описывает расширение", "delivery_invalid_extension")
    declared = {
        (etree.QName(n).localname, (n.text or "").casefold())
        for n in config.findall(f"*/{{{M}}}ChildObjects/*")
    }
    requested = xml(generated["Configuration.xml"])
    required = prop(requested, "ConfigurationExtensionCompatibilityMode")
    actual = prop(config, "ConfigurationExtensionCompatibilityMode")
    if _version(actual) < max(_version(required), (8, 3, 13)):
        refuse("Режим совместимости расширения ниже требуемого", "delivery_compatibility")
    bom = configuration.startswith(b"\xef\xbb\xbf")
    crlf = b"\r\n" in configuration

    def style(value: bytes) -> bytes:
        value = value.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")
        return (b"\xef\xbb\xbf" if bom else b"") + (
            value.replace(b"\n", b"\r\n") if crlf else value
        )

    files, before, summaries = {}, {}, {}
    for name, content in sorted(generated.items()):
        if name == "Configuration.xml":
            continue
        old = read(name)
        changes = []
        if name.endswith(".xml") and not name.endswith("/Content.xml"):
            incoming = xml(content)
            obj = incoming[0]
            kind = etree.QName(obj).localname
            expected = None
            object_name = obj.findtext(f"{{{M}}}Properties/{{{M}}}Name", "").casefold()
            if old is None and (kind, object_name) in declared and name not in owned:
                refuse(
                    "Объект объявлен, но файл описания отсутствует: " + name,
                    "delivery_conflict" if kind == "CommonModule" else "delivery_invalid_extension",
                )
            if kind != "CommonModule":
                source = base / name
                if (
                    not source.is_file()
                    or prop(xml(source.read_bytes()), "ObjectBelonging") == "Adopted"
                ):
                    refuse(
                        "Заимствуемый объект отсутствует в основной конфигурации: " + name,
                        "delivery_missing_object",
                    )
                expected = obj.findtext(f"{{{M}}}Properties/{{{M}}}ExtendedConfigurationObject")
                if xml(source.read_bytes())[0].get("uuid") != expected:
                    refuse(
                        "UUID заимствуемого объекта не совпадает с основной конфигурацией: " + name,
                        "delivery_missing_object",
                    )
            if old is not None:
                current = xml(old)
                if kind == "CommonModule":
                    if name not in owned or owned[name] != old:
                        refuse("Имя общего модуля занято: " + name)
                else:
                    if (
                        prop(current, "ObjectBelonging") != "Adopted"
                        or prop(current, "ExtendedConfigurationObject") != expected
                    ):
                        refuse("Объект в расширении не является нужным заимствованием: " + name)
                    editor = XmlInsert(old)
                    current = editor.root
                    if kind == "ExchangePlan":
                        info = current.find(f"*/{{{M}}}InternalInfo")
                        child = current.find(f"*/{{{M}}}ChildObjects")
                        if info is None or child is None:
                            refuse(
                                "Неполное описание заимствованного плана",
                                "delivery_invalid_extension",
                            )
                        states = {
                            n.findtext(f"{{{XR}}}Property")
                            for n in info.findall(f"{{{XR}}}PropertyState")
                        }
                        added = [
                            n
                            for n in obj.findall(f"{{{M}}}InternalInfo/{{{XR}}}PropertyState")
                            if n.findtext(f"{{{XR}}}Property") not in states
                        ]
                        editor.add(info, added)
                        changes.extend(
                            "PropertyState: " + str(n.findtext(f"{{{XR}}}Property")) for n in added
                        )
                        attributes = {
                            n.findtext(f"{{{M}}}Properties/{{{M}}}Name", "").casefold()
                            for n in child
                        }
                        added = [
                            n
                            for n in obj.findall(f"{{{M}}}ChildObjects/*")
                            if n.findtext(f"{{{M}}}Properties/{{{M}}}Name", "").casefold()
                            not in attributes
                        ]
                        editor.add(child, added)
                        changes.extend(
                            "Attribute: " + str(n.findtext(f"{{{M}}}Properties/{{{M}}}Name"))
                            for n in added
                        )
                    elif kind == "EventSubscription":
                        target = current.find(f"*/{{{M}}}Properties/{{{M}}}Source")
                        source = obj.find(f"{{{M}}}Properties/{{{M}}}Source")
                        if target is None or source is None:
                            refuse(
                                "Нет Source заимствованной подписки", "delivery_invalid_extension"
                            )
                        values = {(n.text or "").casefold() for n in target}
                        added = []
                        for item in source:
                            key = (item.text or "").casefold()
                            if key not in values:
                                added.append(item)
                                values.add(key)
                        editor.add(target, added)
                        changes.extend("Source: " + str(n.text) for n in added)
                    content = editor.result() if editor.edits else old
        elif name.endswith("/Content.xml") and old is not None:
            editor = XmlInsert(old)
            incoming = xml(content)
            ns = etree.QName(incoming).namespace
            for parent in (incoming, *incoming.findall(f"{{{ns}}}ExtensionProperty")):
                target = (
                    editor.root
                    if parent is incoming
                    else editor.root.find(f"{{{ns}}}ExtensionProperty")
                )
                if target is None:
                    editor.add(editor.root, [parent])
                    changes.append("ExtensionProperty")
                    continue
                known = {n.findtext(f"{{{ns}}}Metadata") for n in target.findall(f"{{{ns}}}Item")}
                added = [
                    n
                    for n in parent.findall(f"{{{ns}}}Item")
                    if n.findtext(f"{{{ns}}}Metadata") not in known
                ]
                editor.add(
                    target,
                    added,
                    before=target.find(f"{{{ns}}}ExtensionProperty")
                    if parent is incoming
                    else None,
                )
                changes.extend("Content: " + str(n.findtext(f"{{{ns}}}Metadata")) for n in added)
            content = editor.result() if editor.edits else old
        elif name.endswith("/ManagerModule.bsl"):
            content = _route_merge(old, content, owned.get(name))
            changes.append("Маршрут версии формата")
        elif old is not None and (name not in owned or old != owned[name]):
            refuse("Файл модуля занят или изменён вручную: " + name)
        content = old if old == content else style(content)
        if old is None or old != content or name in owned:
            files[name], before[name], summaries[name] = content, old, changes

    editor = XmlInsert(configuration)
    children = editor.root.find(f"*/{{{M}}}ChildObjects")
    if children is None:
        refuse("Нет ChildObjects расширения", "delivery_invalid_extension")
    known = {(n.tag, n.text) for n in children}
    base_children = xml((base / "Configuration.xml").read_bytes()).find(f"*/{{{M}}}ChildObjects")
    # Порядок видов: PSR/Configuration.xml:46-570; существующие записи не переставляются.
    order = [
        "Language",
        "CommonModule",
        "SessionParameter",
        "Role",
        "CommonAttribute",
        "ExchangePlan",
        "FilterCriterion",
        "EventSubscription",
        "ScheduledJob",
        "FunctionalOption",
        "DefinedType",
        "SettingsStorage",
        "CommonForm",
        "CommonCommand",
        "CommandGroup",
        "CommonTemplate",
        "CommonPicture",
        "XDTOPackage",
        "WebService",
        "HTTPService",
        "WSReference",
        "StyleItem",
        "Style",
        "Subsystem",
        "Interface",
        "Constant",
        "Catalog",
        "Document",
        "DocumentNumerator",
        "Sequence",
        "DocumentJournal",
        "Enum",
        "Report",
        "DataProcessor",
        "ChartOfCharacteristicTypes",
        "ChartOfAccounts",
        "ChartOfCalculationTypes",
        "InformationRegister",
        "AccumulationRegister",
        "AccountingRegister",
        "CalculationRegister",
        "BusinessProcess",
        "Task",
    ]
    if base_children is not None:
        order = (
            list(dict.fromkeys([etree.QName(n).localname for n in base_children] + order))
            if len(base_children) > 20
            else order
        )
    additions = {}
    for item in requested.findall(f"*/{{{M}}}ChildObjects/*"):
        if (item.tag, item.text) in known:
            continue
        rank = order.index(etree.QName(item).localname)
        later = next(
            (
                n
                for n in children
                if etree.QName(n).localname in order
                and order.index(etree.QName(n).localname) > rank
            ),
            None,
        )
        additions.setdefault(later, []).append(item)
    for later, items in additions.items():
        editor.add(children, items, before=later)
    if additions:
        files["Configuration.xml"] = style(editor.result())
        before["Configuration.xml"] = configuration
        summaries["Configuration.xml"] = [
            etree.QName(n).localname + ": " + str(n.text)
            for items in additions.values()
            for n in items
        ]
    return MergeResult(files, before, summaries, inputs)
