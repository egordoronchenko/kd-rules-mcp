"""Неизменяемый снимок менеджера регистрации (§5.2).

Это не XML «ПравилаРегистрации»: проекцию в правила КД 2 строит адаптер
`validation/ed_registration.py`, и её нельзя сохранять как файл правил.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    Coverage,
    DispatcherCase,
    Expr,
    Field,
    ParseStatus,
    Routine,
    SourceFile,
    SourceSpan,
    UnknownFragment,
)

# Событие диспетчера и поле флага в строке правила (шаблон КД 3, Template.txt:48–59).
HANDLER_EVENTS: tuple[tuple[str, str], ...] = (
    ("ПередОбработкой", "ЕстьОбработчикПередОбработкой"),
    ("ПриОбработке", "ЕстьОбработчикПриОбработке"),
    ("ПриОбработкеДополнительный", "ЕстьОбработчикПриОбработкеДополнительный"),
    ("ПослеОбработки", "ЕстьОбработчикПослеОбработки"),
    ("ПакетнаяОбработка", "ПакетноеВыполнениеОбработчиков"),
)


@dataclass(frozen=True, slots=True)
class XmlNode:
    """Узел вложенного XML отбора. Текст и дети не смешиваются с сырым BSL."""

    tag: str
    attrib: tuple[tuple[str, str], ...]
    text: str
    children: tuple[XmlNode, ...]


@dataclass(frozen=True, slots=True)
class FilterTree:
    """Литерал отбора: декодированный XML, его дерево и диапазон литерала в BSL."""

    root_tag: str
    tree: XmlNode | None
    decoded_xml: str
    literal_span: SourceSpan
    literal_raw: str
    decoded_line_map: tuple[int, ...]
    unknown_children: tuple[XmlNode, ...]
    routine_name: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RegistrationParameter:
    """Поле `ПараметрыРегистрации`: имя и выражение, без исполнения BSL."""

    name: str
    field: Field[Expr]
    span: SourceSpan
    raw_text: str


@dataclass(frozen=True, slots=True)
class RegistrationRule:
    """Одна добавленная строка ПРО. `code` — идентификатор, у дублей с суффиксом `#N`."""

    identifier: str
    code: str
    qualified_id: str
    procedure_name: str
    metadata_name: Field[str]
    plan_name: Field[str]
    unload_flag: Field[str]
    empty_object_filter: Field[bool]
    batch: Field[bool]
    handler_flags: tuple[tuple[str, Field[bool]], ...]
    manager_name: Field[Expr]
    plan_filter: FilterTree | None
    object_filter: FilterTree | None
    preserved_assignments: tuple[tuple[str, Expr], ...]
    origins: tuple[SourceSpan, ...]
    span: SourceSpan
    raw_text: str


@dataclass(frozen=True, slots=True)
class RegistrationModuleDocument:
    """Снимок общего модуля регистрации. BSL не исполняется."""

    source_files: tuple[SourceFile, ...]
    parameters: tuple[RegistrationParameter, ...]
    rules: tuple[RegistrationRule, ...]
    routines: tuple[Routine, ...]
    dispatch_cases: tuple[DispatcherCase, ...]
    filter_literals: tuple[FilterTree, ...]
    unknown: tuple[UnknownFragment, ...]
    parse_status: ParseStatus
    coverage: Coverage

    @property
    def source(self) -> SourceFile:
        return self.source_files[0]
