"""Статическая совместимость маршрутов EnterpriseData для пары конфигураций.

Выбор версии и десять проверок `ed.route.*` не читают файлы и не обращаются к сервису:
вызывающий передаёт профили и уже загруженные схемы. Ошибка — только то, что при полном
входе гарантированно не даст обменяться. `ed.route.newer_unregistered` — подсказка.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Protocol

from kd_rules_mcp.ed.address import escape_segment
from kd_rules_mcp.ed.route_model import (
    ManagerInfo,
    PackageInfo,
    PlanRoute,
    RouteEntry,
    RouteProfile,
    RouteStatus,
)
from kd_rules_mcp.ed.routes import _LAYER_REASON, _min_version, _version_parts, compare_versions
from kd_rules_mcp.ed.schema.model import EdSchema, OriginStep, QName, SchemaProperty, SchemaType
from kd_rules_mcp.validation.report import Issue, Level, Skipped, ValidationReport

EXCHANGE_MESSAGE = "http://www.1c.ru/SSL/Exchange/Message"

_CHECKS = (
    "ed.route.empty_intersection",
    "ed.route.manager_missing",
    "ed.route.package_missing",
    "ed.route.newer_unregistered",
    "ed.route.import_unresolved",
    "ed.route.exchange_message_missing",
    "ed.route.schema_diff",
    "ed.route.interface_mismatch",
    "ed.route.registration_mismatch",
    "ed.route.map_sources_differ",
)
_CHECK_INDEX = {name: index for index, name in enumerate(_CHECKS)}
_SELECTED = (
    "ed.route.manager_missing",
    "ed.route.package_missing",
    "ed.route.import_unresolved",
    "ed.route.exchange_message_missing",
    "ed.route.schema_diff",
    "ed.route.interface_mismatch",
    "ed.route.registration_mismatch",
)
_SKIP_INDEX = {
    "ed.route.node_state": -6,
    "ed.route.variant_context": -5,
    "ed.route.extensions": -4,
    "ed.route.reading": -3,
    "ed.route.schema_ambiguous": -2,
    "ed.route.selection": -1,
    **_CHECK_INDEX,
}
# Совместимость подтверждена, только если эти проверки выполнились, а не были пропущены.
_CONFIRMED = (
    ("ed.route.manager_missing", 0),
    ("ed.route.manager_missing", 1),
    ("ed.route.package_missing", 0),
    ("ed.route.package_missing", 1),
    ("ed.route.exchange_message_missing", 0),
    ("ed.route.exchange_message_missing", 1),
    ("ed.route.import_unresolved", 0),
    ("ed.route.import_unresolved", 1),
    ("ed.route.schema_diff", -1),
)
_NODE = (
    "Версия конкретного узла неизвестна; при пустом значении "
    "исполнитель выбирает минимальную версию карты (XDTO:3618)."
)
_EXTENSIONS = "Расширения конфигурации не учитывались; слой base не равен живой базе."
_NO_ROUTE = "Нет выбранного маршрута: проверка выбранной версии не выполнялась."
_EMPTY = "Общая версия обмена не найдена; совпадение пакетов не заменяет карту версий."
_MANAGER = "Версия «{version}» ссылается на отсутствующий общий модуль «{name}» ({file}:{line})."
_PACKAGE = (
    "Для выбранного маршрута не найден пакет пространства «{uri}»; "
    "объектные правила могут быть исключены исполнителем."
)
_NEWER = "Есть более новые пакеты вне карты версий: {versions}; автоматически они не выбираются."
_IMPORT = "Не разрешены импорты выбранной схемы: {uris}; совместимость типов не установлена."
_HEADER = (
    "Отсутствует пакет общего заголовка ExchangeMessage; "
    "выбранный обмен не обеспечен схемой сообщения."
)
_DIFF = (
    "Схемы пространства «{uri}» различаются: типов +{added}/\u2212{removed}/~{changed}; "
    "смотрите страницу schema_diff."
)
_INTERFACE = (
    "Интерфейсы менеджеров различаются: {left}/{right}; "
    "это не означает несовместимость формата сообщений."
)
_REGISTRATION = (
    "Регистрация изменений различается: {left}/{right}; настройте обе стороны независимо."
)
_MAPS = "Карты с узлом и без узла различаются; результат зависит от контекста вызова."
_PAIR = "Пара"
_BETA = "Версия с пометкой beta не выбирается: сравнение с ней всегда проигрывает (XDTO:9176)."
_MINIMUM_UNPROVEN = "минимум версии при пустом узле не подтверждён"


class _SkipFn(Protocol):
    def __call__(self, check: str, reason: str, side: int = -1) -> None: ...


class _AddFn(Protocol):
    def __call__(
        self,
        check: str,
        level: Level,
        address: str,
        message: str,
        side: int = -1,
        uri: str = "",
    ) -> None: ...


class RouteSelectionError(ValueError):
    """Выбор плана неоднозначен или запрещён. Сервис сопоставляет это с invalid_argument."""


@dataclass(frozen=True, slots=True)
class SchemaUnavailable:
    """Схема выбранного URI не передана в сравнение как полная модель."""

    status: str
    reason: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class RouteSelection:
    """Результат `select_route` до загрузки схем. `selected_key` не задаётся снаружи."""

    context: str
    left_profile_id: str
    right_profile_id: str
    left_fingerprint: str
    right_fingerprint: str
    left_plan: str | None
    right_plan: str | None
    common_versions: tuple[str, ...]
    tied_maxima: tuple[str, ...]
    selected_key: str | None
    required_uris: tuple[tuple[str, tuple[str, ...]], ...]
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True, slots=True)
class SideSelection:
    """Аргументы перехода к менеджеру и схеме одной стороны. Путь пакета — описание XML."""

    manager_name: str | None
    manager_path: str | None
    format_version: str | None
    format_uri: str | None
    package_metadata_name: str | None
    package_path: str | None
    schema_imports: tuple[tuple[str, str | None], ...]
    schema_reason: str | None


@dataclass(frozen=True, slots=True)
class PairProfile:
    """Итог пары. `actual_node_version` всегда пуст: версия узла статикой не читается."""

    context: str
    status: str
    quality: str | None
    left_plan: str | None
    right_plan: str | None
    common_versions: tuple[str, ...]
    tied_maxima: tuple[str, ...]
    negotiated_candidate: str | None
    actual_node_version: None
    empty_node_fallback: tuple[str | None, str | None]
    left: SideSelection
    right: SideSelection


@dataclass(frozen=True, slots=True)
class SchemaFieldChange:
    """Одно поле нормализованной схемы. Происхождение поясняет путь и в равенство не входит."""

    type_qname: str
    property_path: str
    field: str
    left: str
    right: str
    left_origin: str
    right_origin: str


@dataclass(frozen=True, slots=True)
class SchemaDiff:
    """Различие одного URI. `added_types` есть справа и нет слева, `removed_types` — наоборот."""

    uri: str
    added_types: tuple[str, ...]
    removed_types: tuple[str, ...]
    changed_types: tuple[str, ...]
    changes: tuple[SchemaFieldChange, ...]


@dataclass(frozen=True, slots=True)
class RouteComparison:
    """Отчёт пары. `report` — существующий накопитель; после возврата его не меняют."""

    profile: PairProfile
    report: ValidationReport
    schema_diffs: tuple[SchemaDiff, ...]


@dataclass(frozen=True, slots=True)
class _MapView:
    plan: PlanRoute | None
    entries: tuple[RouteEntry, ...]
    effective: dict[str, str | None]
    status: RouteStatus
    base: str | None
    empty_fallback: str | None
    ready: bool
    plans_unproven: bool = False


@dataclass(frozen=True, slots=True)
class _IssueRow:
    check: str
    side: int
    uri: str
    level: Level
    address: str
    message: str


def select_route(
    left: RouteProfile,
    right: RouteProfile,
    *,
    context: str = "plan",
    left_plan: str | None = None,
    right_plan: str | None = None,
) -> RouteSelection:
    """Пересечение полных карт и максимум по компаратору исполнителя.

    При неполной карте `common_versions` — только доказанные общие ключи, `selected_key` пуст.
    Равные по компаратору разные строки не упорядочиваются: `tied_maxima` и пропуск выбора.
    """
    if context not in ("plan", "without_node"):
        raise RouteSelectionError("Контекст маршрута: plan или without_node")
    if context == "without_node" and (left_plan is not None or right_plan is not None):
        raise RouteSelectionError("Для контекста без узла планы не задаются")
    left_view = _map_view(left, context, left_plan)
    right_view = _map_view(right, context, right_plan)
    common = _sort_versions(key for key in left_view.effective if key in right_view.effective)
    skips: list[Skipped] = []
    tied: tuple[str, ...] = ()
    selected: str | None = None
    unreadiness = _unreadiness(left_view, right_view, left, right)
    if unreadiness is not None:
        skips.append(Skipped("ed.route.selection", unreadiness))
    else:
        selected, tied, problem = _maximums(common)
        if problem == "tied":
            names = _limit_names(tied)
            skips.append(
                Skipped(
                    "ed.route.selection",
                    "Несколько максимальных версий равны по компаратору исполнителя: "
                    f"{names}; порядок списка корреспондента не задан.",
                )
            )
            selected = None
        elif problem == "beta":
            skips.append(Skipped("ed.route.selection", _BETA))
            selected = None
        elif problem == "unsupported":
            bad = _sort_versions(key for key in common if _version_parts(key) is None)
            skips.append(
                Skipped(
                    "ed.route.selection",
                    f"Неподдержанная форма ключа не сравнивается: {_limit_names(bad)}.",
                )
            )
            selected = None
    uris: list[tuple[str, tuple[str, ...]]] = []
    if context == "plan" and selected is not None:
        for side, view in (("left", left_view), ("right", right_view)):
            uri = _format_uri(view.base, selected)
            if uri is not None:
                uris.append((side, (uri,)))
    return RouteSelection(
        context,
        left.profile_id,
        right.profile_id,
        left.sources_fingerprint,
        right.sources_fingerprint,
        left_view.plan.plan_name if left_view.plan is not None else None,
        right_view.plan.plan_name if right_view.plan is not None else None,
        common,
        tied,
        selected,
        tuple(uris),
        tuple(skips),
    )


def compare_routes(
    left: RouteProfile,
    right: RouteProfile,
    selection: RouteSelection,
    schemas: Mapping[tuple[str, str], EdSchema | SchemaUnavailable],
) -> RouteComparison:
    """Десять проверок пары. Повторно собирает выбор и не принимает чужой `selected_key`."""
    fresh = select_route(
        left,
        right,
        context=selection.context,
        left_plan=selection.left_plan,
        right_plan=selection.right_plan,
    )
    if fresh != selection:
        raise ValueError("Выбор маршрута не согласован с профилями")
    left_view = _map_view(left, selection.context, selection.left_plan)
    right_view = _map_view(right, selection.context, selection.right_plan)
    rows: list[_IssueRow] = []
    skips: list[tuple[int, int, str, str]] = []
    diffs: list[SchemaDiff] = []
    flags = _Flags()

    def skip(check: str, reason: str, side: int = -1) -> None:
        skips.append((_SKIP_INDEX[check], side, check, reason))

    def add(
        check: str, level: Level, address: str, message: str, side: int = -1, uri: str = ""
    ) -> None:
        rows.append(_IssueRow(check, side, uri, level, address, message))

    skip("ed.route.node_state", _NODE)
    _skip_variants(left, right, skip)
    has_layers = any(
        entry.source.layer != "base"
        for profile in (left, right)
        for entry in (
            *profile.without_node_entries,
            *(entry for plan in profile.plans for entry in plan.entries),
            *profile.format_extensions,
        )
    )
    skip("ed.route.extensions", _LAYER_REASON if has_layers else _EXTENSIONS)
    _skip_reading(left, right, skip)
    _skip_ambiguity(left, right, left_view, right_view, selection.selected_key, schemas, skip)
    _skip_unproven_minimum(left_view, right_view, skip)
    for item in selection.skipped:
        skip(item.check, item.reason)

    _check_intersection(selection, left_view, right_view, left, right, add, skip)
    if selection.selected_key is None:
        for check in _SELECTED:
            skip(check, _NO_ROUTE)
    else:
        _check_selected(
            left,
            right,
            left_view,
            right_view,
            selection,
            schemas,
            add,
            skip,
            diffs,
            flags,
        )
    _check_newer(left, left_view, "left", 0, add, skip)
    _check_newer(right, right_view, "right", 1, add, skip)
    _check_map_sources(left, left_view, selection.context, "left", 0, add, skip)
    _check_map_sources(right, right_view, selection.context, "right", 1, add, skip)

    report = _build_report(rows, skips)
    confirmed = all(item in flags.ran for item in _CONFIRMED)
    if report.errors:
        status = "blocked"
    elif flags.package_missing or flags.schema_blocked or not confirmed:
        status = "unknown"
    else:
        status = "statically_compatible"
    profile = PairProfile(
        selection.context,
        status,
        "warnings" if report.warnings else None,
        selection.left_plan,
        selection.right_plan,
        selection.common_versions,
        selection.tied_maxima,
        selection.selected_key,
        None,
        (left_view.empty_fallback, right_view.empty_fallback),
        _side_target(left, left_view, selection, schemas, "left"),
        _side_target(right, right_view, selection, schemas, "right"),
    )
    return RouteComparison(profile, report, tuple(sorted(diffs, key=lambda item: item.uri)))


@dataclass
class _Flags:
    package_missing: bool = False
    schema_blocked: bool = False
    ran: set[tuple[str, int]] = field(default_factory=set)


def _confirm(flags: _Flags, check: str, side: int = -1) -> None:
    flags.ran.add((check, side))


def _check_intersection(
    selection: RouteSelection,
    left_view: _MapView,
    right_view: _MapView,
    left: RouteProfile,
    right: RouteProfile,
    add: _AddFn,
    skip: _SkipFn,
) -> None:
    if not left_view.ready or not right_view.ready:
        reason = _unreadiness(left_view, right_view, left, right) or _NO_ROUTE
        skip("ed.route.empty_intersection", reason)
        return
    if not selection.common_versions:
        add("ed.route.empty_intersection", Level.ERROR, _PAIR, _EMPTY)


def _check_selected(
    left: RouteProfile,
    right: RouteProfile,
    left_view: _MapView,
    right_view: _MapView,
    selection: RouteSelection,
    schemas: Mapping[tuple[str, str], EdSchema | SchemaUnavailable],
    add: _AddFn,
    skip: _SkipFn,
    diffs: list[SchemaDiff],
    flags: _Flags,
) -> None:
    key = selection.selected_key
    assert key is not None
    _check_manager(left, left_view, selection.context, key, "left", 0, add, skip, flags)
    _check_manager(right, right_view, selection.context, key, "right", 1, add, skip, flags)
    left_uri = _format_uri(left_view.base, key)
    right_uri = _format_uri(right_view.base, key)
    _check_package(left, left_view, left_uri, selection.context, key, "left", 0, add, skip, flags)
    _check_package(
        right, right_view, right_uri, selection.context, key, "right", 1, add, skip, flags
    )
    _check_header(left, selection.context, "left", 0, add, skip, flags)
    _check_header(right, selection.context, "right", 1, add, skip, flags)
    _check_schema(
        left,
        right,
        left_view,
        right_view,
        key,
        left_uri,
        right_uri,
        selection.context,
        schemas,
        add,
        skip,
        diffs,
        flags,
    )
    _check_interface(left, right, left_view, right_view, key, add, skip)
    _check_registration(left_view, right_view, selection.context, add, skip)


def _check_manager(
    profile: RouteProfile,
    view: _MapView,
    context: str,
    key: str,
    side: str,
    side_index: int,
    add: _AddFn,
    skip: _SkipFn,
    flags: _Flags,
) -> None:
    name = view.effective.get(key)
    address = _version_address(side, context, view.plan, key)
    if not name:
        skip(
            "ed.route.manager_missing",
            f"{_word(side)}: менеджер выбранной версии не задан литералом.",
            side_index,
        )
        return
    info = _find_manager(profile, name)
    declared = any(item.casefold() == name.casefold() for item in profile.declared_modules)
    missing_body = (info is not None and info.metadata_exists and not info.source_exists) or (
        info is None and declared
    )
    if missing_body:
        skip(
            "ed.route.manager_missing",
            f"{_word(side)}: общий модуль «{name}» объявлен, но тело не прочитано.",
            side_index,
        )
        return
    if info is None or not info.metadata_exists:
        entry = _effective_entry(view.entries, key)
        file = entry.source.relative_file if entry is not None else ""
        line = entry.source.line_start if entry is not None else 0
        add(
            "ed.route.manager_missing",
            Level.ERROR,
            address,
            _MANAGER.format(version=key, name=name, file=file, line=line),
            side_index,
        )
    _confirm(flags, "ed.route.manager_missing", side_index)


def _check_package(
    profile: RouteProfile,
    view: _MapView,
    uri: str | None,
    context: str,
    key: str,
    side: str,
    side_index: int,
    add: _AddFn,
    skip: _SkipFn,
    flags: _Flags,
) -> None:
    if context != "plan":
        skip("ed.route.package_missing", "URI сообщения без узла неизвестен.", side_index)
        return
    if uri is None:
        skip(
            "ed.route.package_missing",
            f"{_word(side)}: URI выбранной версии не определён.",
            side_index,
        )
        return
    gap = _catalog_gap(profile)
    if gap is not None:
        skip("ed.route.package_missing", f"{_word(side)}: {gap}", side_index)
        return
    if any(item.namespace == uri for item in profile.packages):
        _confirm(flags, "ed.route.package_missing", side_index)
        return
    flags.package_missing = True
    add(
        "ed.route.package_missing",
        Level.WARNING,
        _version_address(side, context, view.plan, key),
        _PACKAGE.format(uri=uri),
        side_index,
        uri,
    )
    _confirm(flags, "ed.route.package_missing", side_index)


def _check_header(
    profile: RouteProfile,
    context: str,
    side: str,
    side_index: int,
    add: _AddFn,
    skip: _SkipFn,
    flags: _Flags,
) -> None:
    if context != "plan":
        skip("ed.route.exchange_message_missing", "URI сообщения без узла неизвестен.", side_index)
        return
    gap = _catalog_gap(profile)
    if gap is not None:
        skip("ed.route.exchange_message_missing", f"{_word(side)}: {gap}", side_index)
        return
    matches = [item for item in profile.packages if item.namespace == EXCHANGE_MESSAGE]
    if len(matches) > 1:
        skip(
            "ed.route.exchange_message_missing",
            f"{_word(side)}: неоднозначный пакет пространства «{EXCHANGE_MESSAGE}», "
            "проверка заголовка не выполнялась",
            side_index,
        )
        return
    if matches:
        _confirm(flags, "ed.route.exchange_message_missing", side_index)
        return
    add(
        "ed.route.exchange_message_missing",
        Level.ERROR,
        f"{_word(side)}/Пакет/{escape_segment(EXCHANGE_MESSAGE)}",
        _HEADER,
        side_index,
        EXCHANGE_MESSAGE,
    )
    _confirm(flags, "ed.route.exchange_message_missing", side_index)


def _check_schema(
    left: RouteProfile,
    right: RouteProfile,
    left_view: _MapView,
    right_view: _MapView,
    key: str,
    left_uri: str | None,
    right_uri: str | None,
    context: str,
    schemas: Mapping[tuple[str, str], EdSchema | SchemaUnavailable],
    add: _AddFn,
    skip: _SkipFn,
    diffs: list[SchemaDiff],
    flags: _Flags,
) -> None:
    if context != "plan":
        skip("ed.route.schema_diff", "URI сообщения без узла неизвестен.")
        skip("ed.route.import_unresolved", "URI сообщения без узла неизвестен.")
        return
    if left_uri is None or right_uri is None:
        skip("ed.route.schema_diff", "URI выбранной версии не определён.")
        skip("ed.route.import_unresolved", "URI выбранной версии не определён.")
        flags.schema_blocked = True
        return
    if left_uri != right_uri:
        skip(
            "ed.route.schema_diff",
            f"Разные URI маршрута: {left_uri} и {right_uri}. "
            "Сравнение одного пространства неприменимо.",
        )
        flags.schema_blocked = True
    left_schema = _lookup_schema(schemas, "left", left_uri)
    right_schema = _lookup_schema(schemas, "right", right_uri)
    _check_imports(
        left,
        left_schema,
        _version_address("left", context, left_view.plan, key),
        "left",
        0,
        add,
        skip,
        flags,
    )
    _check_imports(
        right,
        right_schema,
        _version_address("right", context, right_view.plan, key),
        "right",
        1,
        add,
        skip,
        flags,
    )
    if left_uri != right_uri:
        return
    for label, value in (("Левая", left_schema), ("Правая", right_schema)):
        problem = _schema_problem(left if label == "Левая" else right, value, left_uri)
        if problem is not None:
            skip("ed.route.schema_diff", f"{label}: {problem}")
            flags.schema_blocked = True
            return
    assert isinstance(left_schema, EdSchema) and isinstance(right_schema, EdSchema)
    _confirm(flags, "ed.route.schema_diff")
    for uri in sorted(set(_package_uris(left_schema)) | set(_package_uris(right_schema))):
        diff = _diff_uri(left_schema, right_schema, uri)
        if diff is None:
            continue
        diffs.append(diff)
        add(
            "ed.route.schema_diff",
            Level.WARNING,
            f"{_PAIR}/Схема/{escape_segment(uri)}",
            _DIFF.format(
                uri=uri,
                added=len(diff.added_types),
                removed=len(diff.removed_types),
                changed=len(diff.changed_types),
            ),
            uri=uri,
        )


def _check_imports(
    profile: RouteProfile,
    schema: EdSchema | SchemaUnavailable,
    address: str,
    side: str,
    side_index: int,
    add: _AddFn,
    skip: _SkipFn,
    flags: _Flags,
) -> None:
    if isinstance(schema, SchemaUnavailable):
        skip("ed.route.import_unresolved", f"{_word(side)}: {schema.reason}", side_index)
        return
    hide_message = _catalog_gap(profile) is None and not any(
        item.namespace == EXCHANGE_MESSAGE for item in profile.packages
    )
    missing: list[str] = []
    for package in schema.packages:
        for item in package.imports:
            if item.status == "resolved":
                continue
            if hide_message and item.namespace == EXCHANGE_MESSAGE:
                continue
            if item.namespace not in missing:
                missing.append(item.namespace)
    _confirm(flags, "ed.route.import_unresolved", side_index)
    if not missing:
        return
    missing.sort()
    add(
        "ed.route.import_unresolved",
        Level.WARNING,
        address,
        _IMPORT.format(uris=_limit_names(missing)),
        side_index,
    )


def _check_interface(
    left: RouteProfile,
    right: RouteProfile,
    left_view: _MapView,
    right_view: _MapView,
    key: str,
    add: _AddFn,
    skip: _SkipFn,
) -> None:
    pair = (
        _interface(left, left_view, key),
        _interface(right, right_view, key),
    )
    if pair[0] is None or pair[1] is None:
        skip("ed.route.interface_mismatch", "Интерфейс менеджера неизвестен.")
        return
    if pair[0] != pair[1]:
        add(
            "ed.route.interface_mismatch",
            Level.WARNING,
            _PAIR,
            _INTERFACE.format(left=pair[0], right=pair[1]),
        )


def _check_registration(
    left_view: _MapView, right_view: _MapView, context: str, add: _AddFn, skip: _SkipFn
) -> None:
    if context != "plan":
        skip(
            "ed.route.registration_mismatch", "Без узла способы регистрации планов не сравниваются."
        )
        return
    if left_view.plan is None or right_view.plan is None:
        skip("ed.route.registration_mismatch", "Нет выбранного плана для сравнения регистрации.")
        return
    modes = (left_view.plan.registration.mode, right_view.plan.registration.mode)
    if modes[0] not in ("xml", "manager") or modes[1] not in ("xml", "manager"):
        skip("ed.route.registration_mismatch", "Способ регистрации неизвестен.")
        return
    if modes[0] != modes[1]:
        add(
            "ed.route.registration_mismatch",
            Level.WARNING,
            _PAIR,
            _REGISTRATION.format(left=modes[0], right=modes[1]),
        )


def _check_newer(
    profile: RouteProfile, view: _MapView, side: str, side_index: int, add: _AddFn, skip: _SkipFn
) -> None:
    check = "ed.route.newer_unregistered"
    if not view.ready:
        skip(
            check, f"{_word(side)}: карта неполная, более новые пакеты не оценивались.", side_index
        )
        return
    if not view.effective:
        skip(check, f"{_word(side)}: максимум пустой карты не определён.", side_index)
        return
    if view.base is None:
        skip(check, f"{_word(side)}: основание URI формата неизвестно.", side_index)
        return
    if any(_version_parts(key) is None for key in view.effective):
        skip(
            check,
            f"{_word(side)}: неподдержанная форма ключа, максимум карты не определён.",
            side_index,
        )
        return
    chosen, tied, problem = _maximums(tuple(view.effective))
    if problem == "beta":
        skip(check, f"{_word(side)}: {_BETA}", side_index)
        return
    ceiling = tied if problem == "tied" else ((chosen,) if chosen else ())
    if not ceiling:
        skip(check, f"{_word(side)}: максимум карты не определён.", side_index)
        return
    gap = _catalog_gap(profile)
    if gap is not None:
        skip(check, f"{_word(side)}: {gap}", side_index)
        return
    newer: list[str] = []
    for package in profile.packages:
        tail = _version_tail(package.namespace, view.base)
        if tail is None or tail in view.effective or not _strictly_greater(tail, ceiling):
            continue
        if tail not in newer:
            newer.append(tail)
    if not newer:
        return
    add(
        check,
        Level.WARNING,
        _plan_address(side, view.plan.plan_name if view.plan is not None else None),
        _NEWER.format(versions=_limit_names(_sort_versions(newer))),
        side_index,
    )


def _check_map_sources(
    profile: RouteProfile,
    view: _MapView,
    context: str,
    side: str,
    side_index: int,
    add: _AddFn,
    skip: _SkipFn,
) -> None:
    check = "ed.route.map_sources_differ"
    plan = view.plan
    if context != "plan":
        plans = _ed_plans(profile)
        if plans is None:
            skip(check, f"{_word(side)}: набор планов не доказан.", side_index)
            return
        if len(plans) != 1:
            reason = (
                "нет плана для сравнения с глобальным callback."
                if not plans
                else "несколько планов, карта с узлом для сравнения не выбрана."
            )
            skip(check, f"{_word(side)}: {reason}", side_index)
            return
        plan = plans[0]
    if plan is None:
        if view.plans_unproven:
            skip(check, f"{_word(side)}: {_unproven_plans_text(profile)}.", side_index)
        else:
            skip(
                check,
                f"{_word(side)}: нет плана для сравнения с глобальным callback.",
                side_index,
            )
        return
    plan_ready = plan.status == "complete" and not any(
        item.state == "conditional" for item in plan.entries
    )
    global_ready = profile.without_node_status == "complete" and not any(
        item.state == "conditional" for item in profile.without_node_entries
    )
    if not plan_ready or not global_ready:
        skip(check, f"{_word(side)}: карта с узлом или без узла неполная.", side_index)
        return
    if not _same_version_map(plan.effective_map(), profile.effective_without_node()):
        add(check, Level.WARNING, _plan_address(side, plan.plan_name), _MAPS, side_index)


def _map_view(profile: RouteProfile, context: str, plan_name: str | None) -> _MapView:
    if context == "without_node":
        entries = profile.without_node_entries
        ready = profile.without_node_status == "complete" and not any(
            item.state == "conditional" for item in entries
        )
        return _MapView(
            None,
            entries,
            dict(profile.effective_without_node()),
            profile.without_node_status,
            None,
            _proven_minimum(entries, ready),
            ready,
        )
    if plan_name is not None:
        return _named_plan_view(profile, plan_name)
    plans = _ed_plans(profile)
    if plans is None:
        return _MapView(None, (), {}, "partial", None, None, False, plans_unproven=True)
    plan = _choose_plan(plans)
    if plan is None:
        return _MapView(None, (), {}, "complete", None, None, True)
    return _plan_view(plan)


def _plan_view(plan: PlanRoute) -> _MapView:
    ready = plan.status == "complete" and not any(
        item.state == "conditional" for item in plan.entries
    )
    return _MapView(
        plan,
        plan.entries,
        dict(plan.effective_map()),
        plan.status,
        plan.base_namespace,
        plan.empty_node_fallback if ready else None,
        ready,
    )


def _named_plan_view(profile: RouteProfile, name: str) -> _MapView:
    """Явное имя ищется среди всех планов, до вывода о полноте соседних.

    Имена метаданных 1С регистр не различают. В отчёте остаётся написание из конфигурации.
    """
    folded = name.casefold()
    found = [plan for plan in profile.plans if plan.plan_name.casefold() == folded]
    if not found:
        raise RouteSelectionError(f"План обмена «{name}» не найден")
    if len(found) > 1:
        shown = ", ".join(plan.plan_name for plan in found)
        raise RouteSelectionError(f"Несколько планов обмена с именем «{name}»: {shown}")
    plan = found[0]
    if plan.is_ed is False:
        raise RouteSelectionError(
            f"План обмена «{plan.plan_name}» не обменивается через универсальный формат"
        )
    if plan.is_ed is not True:
        return _MapView(
            plan,
            plan.entries,
            {},
            "partial",
            plan.base_namespace,
            None,
            False,
        )
    return _plan_view(plan)


def _ed_plans(profile: RouteProfile) -> tuple[PlanRoute, ...] | None:
    if any(plan.is_ed is None for plan in profile.plans):
        return None
    return tuple(plan for plan in profile.plans if plan.is_ed is True)


def _choose_plan(plans: tuple[PlanRoute, ...]) -> PlanRoute | None:
    if len(plans) > 1:
        shown = ", ".join(plan.plan_name for plan in plans[:3])
        extra = len(plans) - 3
        suffix = f" (ещё {extra})" if extra > 0 else ""
        raise RouteSelectionError(
            f"Несколько планов обмена через универсальный формат: {shown}{suffix}"
        )
    if len(plans) == 1:
        return plans[0]
    return None


def _unreadiness(
    left: _MapView,
    right: _MapView,
    left_profile: RouteProfile | None = None,
    right_profile: RouteProfile | None = None,
) -> str | None:
    parts: list[str] = []
    for label, view, profile in (
        ("Левая", left, left_profile),
        ("Правая", right, right_profile),
    ):
        if view.ready:
            continue
        hint = _map_reading_hint(profile) if profile is not None else ""
        if view.status == "complete":
            parts.append(
                f"{label}: динамические условия, состав карты не доказан; {_MINIMUM_UNPROVEN}"
            )
        elif view.plans_unproven and profile is not None:
            parts.append(f"{label}: {_unproven_plans_text(profile)}; {_MINIMUM_UNPROVEN}")
        else:
            parts.append(f"{label}: карта неполная{hint}; {_MINIMUM_UNPROVEN}")
    if not parts:
        return None
    return "Максимальная общая версия и пустое пересечение не доказаны: " + "; ".join(parts) + "."


def _maximums(keys: Sequence[str]) -> tuple[str | None, tuple[str, ...], str | None]:
    unique = list(dict.fromkeys(keys))
    if not unique:
        return None, (), None
    if any(_version_parts(key) is None for key in unique):
        return None, (), "unsupported"
    # Поиск максимума в БСП начинает с «0.0» и берёт версию при «не меньше».
    # Сравнение с beta всегда возвращает проигрыш beta (XDTO:9176), поэтому beta не выбирается.
    selectable = [key for key in unique if not _is_beta(key)]
    if not selectable:
        return None, (), "beta"
    best: list[str] = []
    for key in selectable:
        if not best:
            best = [key]
            continue
        compared = compare_versions(key, best[0])
        if compared is None:
            return None, (), "unsupported"
        if compared > 0:
            best = [key]
        elif compared == 0 and key not in best:
            best.append(key)
    if len(best) > 1:
        return None, _sort_versions(best), "tied"
    return best[0], (), None


def _strictly_greater(candidate: str, ceiling: Sequence[str]) -> bool:
    if _version_parts(candidate) is None or not ceiling or _is_beta(candidate):
        return False
    for item in ceiling:
        if _is_beta(item):
            return False
        compared = compare_versions(candidate, item)
        if compared is None or compared <= 0:
            return False
    return True


def _is_beta(value: str) -> bool:
    parts = _version_parts(value)
    return parts is not None and len(parts) == 3 and parts[2] == "beta"


def _sort_versions(keys: Iterable[str]) -> tuple[str, ...]:
    def compare(left: str, right: str) -> int:
        if left == right:
            return 0
        left_beta, right_beta = _is_beta(left), _is_beta(right)
        if left_beta and right_beta:
            return -1 if left < right else 1
        if left_beta or right_beta:
            return -1 if left_beta else 1
        if _version_parts(left) is None or _version_parts(right) is None:
            return -1 if left < right else 1
        compared = compare_versions(left, right)
        if compared is None or compared == 0:
            return -1 if left < right else 1
        return compared

    return tuple(sorted(dict.fromkeys(keys), key=cmp_to_key(compare)))


def _version_tail(namespace: str, base: str) -> str | None:
    prefix = f"{base}/"
    if not namespace.startswith(prefix):
        return None
    tail = namespace[len(prefix) :]
    if not tail or "/" in tail or _version_parts(tail) is None:
        return None
    return tail


def _format_uri(base: str | None, key: str | None) -> str | None:
    if not base or not key:
        return None
    return f"{base}/{key}"


def _proven_minimum(entries: Sequence[RouteEntry], ready: bool) -> str | None:
    """Минимум пустой версии узла. Неполная карта число без пометки не получает."""
    if not ready:
        return None
    fallback, _tied = _min_version(entries)
    return fallback


def _same_version_map(left: Mapping[str, str | None], right: Mapping[str, str | None]) -> bool:
    if left.keys() != right.keys():
        return False
    for key, name in left.items():
        other = right[key]
        if name is None or other is None:
            if name is not other:
                return False
            continue
        if name.casefold() != other.casefold():
            return False
    return True


def _package_file(relative: str) -> bool:
    return relative.replace("\\", "/").startswith("XDTOPackages/")


def _limit_names(names: Sequence[str]) -> str:
    shown = list(names[:3])
    text = ", ".join(shown)
    rest = len(names) - len(shown)
    if rest:
        text += f" (ещё {rest})"
    return text


def _word(side: str) -> str:
    return "Левая" if side == "left" else "Правая"


def _version_address(side: str, context: str, plan: PlanRoute | None, key: str) -> str:
    label = _word(side)
    if context == "without_node" or plan is None:
        return f"{label}/БезУзла/Версия/{escape_segment(key)}"
    return f"{label}/План/{escape_segment(plan.plan_name)}/Версия/{escape_segment(key)}"


def _plan_address(side: str, plan_name: str | None) -> str:
    label = _word(side)
    if plan_name is None:
        return f"{label}/БезУзла"
    return f"{label}/План/{escape_segment(plan_name)}"


def _effective_entry(entries: Sequence[RouteEntry], key: str) -> RouteEntry | None:
    found = [item for item in entries if item.state == "effective" and item.key == key]
    return found[-1] if found else None


def _find_manager(profile: RouteProfile, name: str) -> ManagerInfo | None:
    for item in profile.managers:
        if item.name.casefold() == name.casefold():
            return item
    return None


def _interface(profile: RouteProfile, view: _MapView, key: str) -> int | None:
    name = view.effective.get(key)
    if not name:
        return None
    info = _find_manager(profile, name)
    if info is None or info.interface_version is None or info.interface_origin == "unknown":
        return None
    return info.interface_version


def _catalog_gap(profile: RouteProfile) -> str | None:
    gaps = [
        item
        for item in profile.skipped
        if item.code == "ed.route.reading" and _package_file(item.relative_file)
    ]
    if not gaps:
        return None
    places = ", ".join(f"{item.relative_file}:{item.line}" for item in gaps[:3])
    return f"каталог пакетов неполон: {places}"


def _map_reading_hint(profile: RouteProfile) -> str:
    places = [
        f"{item.relative_file}:{item.line}"
        for item in profile.skipped
        if item.code == "ed.route.reading"
        and item.relative_file
        and not _package_file(item.relative_file)
    ]
    if not places:
        return ""
    return f" ({_limit_names(places)})"


def _skip_reading(left: RouteProfile, right: RouteProfile, skip: _SkipFn) -> None:
    details: list[str] = []
    for label, profile in (("Левая", left), ("Правая", right)):
        for item in profile.skipped:
            if item.code != "ed.route.reading":
                continue
            place = f"{item.relative_file}:{item.line}" if item.relative_file else "без места"
            details.append(f"{label}: {place}")
    if not details:
        return
    skip(
        "ed.route.reading",
        f"Пропуски чтения: {len(details)}; первые: {', '.join(details[:3])}",
    )


def _unproven_plans_text(profile: RouteProfile) -> str:
    """Набор планов обмена через формат не доказан: файл и подсказка задать имя."""
    places = [
        f"{item.relative_file}:{item.line}"
        for item in profile.skipped
        if item.code == "ed.route.reading"
        and item.relative_file
        and item.relative_file.replace("\\", "/").startswith("ExchangePlans/")
    ]
    shown = _limit_names(places) if places else "файл плана не указан"
    return f"не доказан набор планов обмена через формат ({shown}); задайте left_plan / right_plan"


def _skip_unproven_minimum(left: _MapView, right: _MapView, skip: _SkipFn) -> None:
    """Полная карта с неподдержанным ключом не подтверждает минимум пустого узла."""
    for label, view in (("Левая", left), ("Правая", right)):
        if not view.ready:
            continue
        bad = [key for key in view.effective if _version_parts(key) is None]
        if not bad:
            continue
        skip(
            "ed.route.selection",
            f"{label}: {_MINIMUM_UNPROVEN}: неподдержанная форма ключа "
            f"({_limit_names(_sort_versions(bad))}).",
        )


def _import_closure(schema: EdSchema) -> set[str]:
    """Базовый URI и пространства, достижимые по импортам переданной схемы."""
    links: dict[str, list[str]] = {}
    for package in schema.packages:
        bucket = links.setdefault(package.namespace, [])
        for item in package.imports:
            bucket.append(item.namespace)
    seen: set[str] = set()
    stack = [schema.base_namespace]
    while stack:
        namespace = stack.pop()
        if namespace in seen:
            continue
        seen.add(namespace)
        stack.extend(links.get(namespace, ()))
    return seen


def _route_namespaces(
    view: _MapView,
    key: str | None,
    schemas: Mapping[tuple[str, str], EdSchema | SchemaUnavailable],
    side: str,
) -> set[str]:
    """Пространства маршрута: выбранный URI, заголовок и замыкание импортов схемы."""
    relevant = {EXCHANGE_MESSAGE}
    uri = _format_uri(view.base, key) if key else None
    if not uri:
        return relevant
    relevant.add(uri)
    value = schemas.get((side, uri))
    if isinstance(value, EdSchema):
        relevant.update(_import_closure(value))
    return relevant


def _duplicate_namespaces(profile: RouteProfile) -> set[str]:
    counts: dict[str, int] = {}
    for package in profile.packages:
        counts[package.namespace] = counts.get(package.namespace, 0) + 1
    return {namespace for namespace, count in counts.items() if count > 1}


def _skip_ambiguity(
    left: RouteProfile,
    right: RouteProfile,
    left_view: _MapView,
    right_view: _MapView,
    key: str | None,
    schemas: Mapping[tuple[str, str], EdSchema | SchemaUnavailable],
    skip: _SkipFn,
) -> None:
    relevant = _route_namespaces(left_view, key, schemas, "left") | _route_namespaces(
        right_view, key, schemas, "right"
    )
    details: list[str] = []
    for label, profile in (("Левая", left), ("Правая", right)):
        if not any(item.code == "ed.route.schema_ambiguous" for item in profile.skipped):
            continue
        groups: dict[str, list[PackageInfo]] = {}
        for package in profile.packages:
            groups.setdefault(package.namespace, []).append(package)
        for namespace, packages in groups.items():
            if len(packages) < 2 or namespace not in relevant:
                continue
            places = ", ".join(f"{item.description_path}:1" for item in packages[:3])
            details.append(f"{label}: {namespace} ({places})")
    if not details:
        return
    skip(
        "ed.route.schema_ambiguous",
        f"Неоднозначное пространство: {len(details)}; первые: {'; '.join(details[:3])}",
    )


def _skip_variants(left: RouteProfile, right: RouteProfile, skip: _SkipFn) -> None:
    details: list[str] = []
    for label, profile in (("Левая", left), ("Правая", right)):
        for item in profile.skipped:
            if item.code == "ed.route.variant_context" and item.relative_file:
                details.append(f"{label}: {item.relative_file}:{item.line}")
    if not details:
        return
    head = ", ".join(details[:3])
    skip(
        "ed.route.variant_context",
        f"Непрозрачные условия вариантов: {len(details)}; первые: {head}",
    )


def _lookup_schema(
    schemas: Mapping[tuple[str, str], EdSchema | SchemaUnavailable], side: str, uri: str
) -> EdSchema | SchemaUnavailable:
    value = schemas.get((side, uri))
    if value is None:
        return SchemaUnavailable("missing", f"Схема «{uri}» не передана", uri)
    if isinstance(value, EdSchema) and value.base_namespace != uri:
        return SchemaUnavailable(
            "unreadable",
            f"Схема «{value.base_namespace}» не соответствует пространству «{uri}»",
            uri,
        )
    return value


def _schema_problem(
    profile: RouteProfile, value: EdSchema | SchemaUnavailable, uri: str
) -> str | None:
    ambiguous = _duplicate_namespaces(profile)
    if uri in ambiguous:
        return f"неоднозначный пакет пространства «{uri}», сравнение схемы не выполнялось"
    if isinstance(value, EdSchema):
        for namespace in sorted(_import_closure(value) - {uri}):
            if namespace in ambiguous:
                return (
                    f"неоднозначный пакет пространства «{namespace}», "
                    "сравнение схемы не выполнялось"
                )
    if isinstance(value, SchemaUnavailable):
        return value.reason
    if value.status != "complete":
        head = "; ".join(f"{item.code}: {item.message}" for item in value.diagnostics[:3])
        return f"схема неполная: {head}" if head else "схема неполная"
    if any(item.status != "complete" for item in value.types.values()):
        return "схема неполная: тип без полного описания"
    return None


def _package_uris(schema: EdSchema) -> set[str]:
    return {package.namespace for package in schema.packages}


def _named(schema: EdSchema, uri: str) -> dict[str, SchemaType]:
    return {
        str(name): typ
        for name, typ in schema.types.items()
        if name.namespace == uri and typ.qname is not None
    }


def _diff_uri(left: EdSchema, right: EdSchema, uri: str) -> SchemaDiff | None:
    left_types = _named(left, uri)
    right_types = _named(right, uri)
    added = tuple(sorted(set(right_types) - set(left_types)))
    removed = tuple(sorted(set(left_types) - set(right_types)))
    changes: list[SchemaFieldChange] = []
    changed: list[str] = []
    import_change = _import_change(left, right, uri)
    if import_change is not None:
        changes.append(import_change)
    for name in sorted(set(left_types) & set(right_types)):
        left_type = left_types[name]
        right_type = right_types[name]
        if _type_view(left, left_type) == _type_view(right, right_type):
            continue
        changed.append(name)
        changes.extend(_diff_type(left, right, left_type, right_type))
    if not added and not removed and not changed and not changes:
        return None
    return SchemaDiff(uri, added, removed, tuple(changed), tuple(changes))


def _import_change(left: EdSchema, right: EdSchema, uri: str) -> SchemaFieldChange | None:
    left_set = _import_namespaces(left, uri)
    right_set = _import_namespaces(right, uri)
    if left_set == right_set:
        return None
    return SchemaFieldChange(
        "",
        "",
        "imports",
        " ".join(left_set),
        " ".join(right_set),
        "",
        "",
    )


def _import_namespaces(schema: EdSchema, uri: str) -> tuple[str, ...]:
    for package in schema.packages:
        if package.namespace == uri:
            return tuple(sorted(item.namespace for item in package.imports))
    return ()


def _type_view(schema: EdSchema, typ: SchemaType, seen: frozenset[str] = frozenset()) -> tuple:
    return (
        typ.kind,
        _qname(typ.base),
        tuple(_qname(item) for item in typ.members),
        typ.variety,
        _properties_view(schema, typ, seen),
        _facets_view(typ),
        typ.open,
        typ.abstract,
        typ.ordered,
        typ.sequenced,
    )


def _order_key(value: object) -> tuple:
    """Ключ сортировки: `None` (unbounded) сравнивается с числом, не роняя TypeError."""
    if isinstance(value, tuple):
        return tuple(_order_key(part) for part in value)
    if value is None:
        return (1,)
    if isinstance(value, bool):
        return (0, value)
    if isinstance(value, int):
        return (0, value)
    return (0, str(value))


def _properties_view(schema: EdSchema, typ: SchemaType, seen: frozenset[str]) -> tuple:
    signed = tuple(_property_view(schema, prop, seen) for prop in typ.properties)
    if not typ.ordered and not typ.sequenced:
        return tuple(sorted(signed, key=_order_key))
    return signed


def _effective_form(value: str | None) -> str:
    """Отсутствие атрибута и явное Element — одна форма свойства XDTO."""
    if value is None or value == "":
        return "element"
    return value.casefold()


def _property_view(schema: EdSchema, prop: SchemaProperty, seen: frozenset[str]) -> tuple:
    return (
        str(prop.name),
        _type_ref_view(schema, prop, seen),
        prop.lower,
        prop.upper,
        prop.nillable,
        _effective_form(prop.form),
    )


def _type_ref_view(schema: EdSchema, prop: SchemaProperty, seen: frozenset[str]) -> tuple:
    if prop.type_ref is not None:
        return ("ref", _qname(prop.type_ref))
    if prop.type_id is None:
        return ("none",)
    typ = schema.by_id.get(prop.type_id)
    if typ is None:
        return ("missing",)
    if typ.qname is not None:
        return ("named", _qname(typ.qname))
    if prop.type_id in seen:
        return ("cycle",)
    return ("anon", _type_view(schema, typ, seen | {prop.type_id}))


def _facets_view(typ: SchemaType) -> tuple:
    return tuple(
        sorted(
            (facet.kind, facet.lexical, _qname(facet.value_type), _fixed(facet.fixed))
            for facet in typ.facets
        )
    )


def _diff_type(
    left_schema: EdSchema,
    right_schema: EdSchema,
    left: SchemaType,
    right: SchemaType,
    *,
    type_qname: str | None = None,
    prefix: str = "",
) -> tuple[SchemaFieldChange, ...]:
    changes: list[SchemaFieldChange] = []
    qname = _qname(left.qname) if type_qname is None else type_qname
    left_origin = _origin(left.origin)
    right_origin = _origin(right.origin)

    def add(
        path: str, field_name: str, left_value: str, right_value: str, lorig: str, rorig: str
    ) -> None:
        full = prefix if not path else (f"{prefix}/{path}" if prefix else path)
        if left_value != right_value:
            changes.append(
                SchemaFieldChange(qname, full, field_name, left_value, right_value, lorig, rorig)
            )

    add("", "kind", left.kind, right.kind, left_origin, right_origin)
    add("", "base", _qname(left.base), _qname(right.base), left_origin, right_origin)
    add(
        "",
        "members",
        " ".join(_qname(item) for item in left.members),
        " ".join(_qname(item) for item in right.members),
        left_origin,
        right_origin,
    )
    add("", "variety", left.variety, right.variety, left_origin, right_origin)
    add("", "open", _bool(left.open), _bool(right.open), left_origin, right_origin)
    add("", "abstract", _bool(left.abstract), _bool(right.abstract), left_origin, right_origin)
    add("", "ordered", _bool(left.ordered), _bool(right.ordered), left_origin, right_origin)
    add("", "sequenced", _bool(left.sequenced), _bool(right.sequenced), left_origin, right_origin)
    add("", "facets", _facet_text(left), _facet_text(right), left_origin, right_origin)
    changes.extend(_diff_properties(left_schema, right_schema, left, right, qname, prefix))
    return tuple(changes)


def _diff_properties(
    left_schema: EdSchema,
    right_schema: EdSchema,
    left: SchemaType,
    right: SchemaType,
    type_qname: str,
    prefix: str = "",
) -> tuple[SchemaFieldChange, ...]:
    changes: list[SchemaFieldChange] = []
    left_props = list(left.properties)
    right_props = list(right.properties)
    if (left.ordered or left.sequenced) and (right.ordered or right.sequenced):
        left_names = [prop.name.local for prop in left.properties]
        right_names = [prop.name.local for prop in right.properties]
        if left_names != right_names and sorted(left_names) == sorted(right_names):
            changes.append(
                SchemaFieldChange(
                    type_qname,
                    prefix,
                    "order",
                    ", ".join(left_names),
                    ", ".join(right_names),
                    _origin(left.origin),
                    _origin(right.origin),
                )
            )
    if not left.ordered and not left.sequenced:
        left_props.sort(key=lambda prop: str(prop.name))
    if not right.ordered and not right.sequenced:
        right_props.sort(key=lambda prop: str(prop.name))
    left_groups = _group_properties(left_props)
    right_groups = _group_properties(right_props)
    for name in sorted(set(left_groups) | set(right_groups)):
        path = f"{prefix}/{name}" if prefix else name
        left_list = left_groups.get(name, [])
        right_list = right_groups.get(name, [])
        if not left_list or not right_list:
            changes.append(
                SchemaFieldChange(
                    type_qname,
                    path,
                    "property",
                    name if left_list else "",
                    name if right_list else "",
                    _origin(left_list[0].origin) if left_list else "",
                    _origin(right_list[0].origin) if right_list else "",
                )
            )
            continue
        for left_prop, right_prop in zip(left_list, right_list, strict=False):
            changes.extend(
                _diff_property(left_schema, right_schema, left_prop, right_prop, type_qname, path)
            )
        if len(left_list) != len(right_list):
            changes.append(
                SchemaFieldChange(
                    type_qname,
                    path,
                    "property_count",
                    str(len(left_list)),
                    str(len(right_list)),
                    "",
                    "",
                )
            )
    return tuple(changes)


def _diff_property(
    left_schema: EdSchema,
    right_schema: EdSchema,
    left: SchemaProperty,
    right: SchemaProperty,
    type_qname: str,
    path: str,
) -> tuple[SchemaFieldChange, ...]:
    changes: list[SchemaFieldChange] = []
    left_origin = _origin(left.origin)
    right_origin = _origin(right.origin)

    def add(field: str, left_value: str, right_value: str) -> None:
        if left_value != right_value:
            changes.append(
                SchemaFieldChange(
                    type_qname, path, field, left_value, right_value, left_origin, right_origin
                )
            )

    add("lower", str(left.lower), str(right.lower))
    add("upper", _upper(left.upper), _upper(right.upper))
    add("nillable", _bool(left.nillable), _bool(right.nillable))
    add("form", _effective_form(left.form), _effective_form(right.form))
    left_type = _type_ref_view(left_schema, left, frozenset())
    right_type = _type_ref_view(right_schema, right, frozenset())
    if left_type != right_type:
        left_anon = _anonymous(left_schema, left)
        right_anon = _anonymous(right_schema, right)
        if left_anon is not None and right_anon is not None:
            changes.extend(
                _diff_type(
                    left_schema,
                    right_schema,
                    left_anon,
                    right_anon,
                    type_qname=type_qname,
                    prefix=path,
                )
            )
        else:
            add("type", _type_text(left_type), _type_text(right_type))
    return tuple(changes)


def _anonymous(schema: EdSchema, prop: SchemaProperty) -> SchemaType | None:
    if prop.type_id is None:
        return None
    typ = schema.by_id.get(prop.type_id)
    if typ is None or typ.qname is not None:
        return None
    return typ


def _group_properties(props: Sequence[SchemaProperty]) -> dict[str, list[SchemaProperty]]:
    groups: dict[str, list[SchemaProperty]] = {}
    for prop in props:
        groups.setdefault(prop.name.local, []).append(prop)
    return groups


def _facet_text(typ: SchemaType) -> str:
    return "; ".join(
        f"{kind}:{lexical}:{value_type}:{fixed}"
        for kind, lexical, value_type, fixed in _facets_view(typ)
    )


def _type_text(view: tuple) -> str:
    return " ".join(str(part) for part in view)


def _qname(value: QName | None) -> str:
    return "" if value is None else str(value)


def _origin(steps: tuple[OriginStep, ...]) -> str:
    return ">".join(f"{step.role}:{step.namespace}:{step.span.line}" for step in steps)


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _upper(value: int | None) -> str:
    return "unbounded" if value is None else str(value)


def _fixed(value: bool | None) -> str:
    if value is None:
        return ""
    return _bool(value)


def _side_target(profile, view, selection, schemas, side: str) -> SideSelection:
    key = selection.selected_key
    uri = _format_uri(view.base, key) if selection.context == "plan" else None
    name = view.effective.get(key) if key is not None else None
    info = _find_manager(profile, name) if name else None
    matches = [item for item in profile.packages if uri is not None and item.namespace == uri]
    package: PackageInfo | None = matches[0] if len(matches) == 1 else None
    value = _lookup_schema(schemas, side, uri) if uri is not None else None
    reason: str | None = None
    imports: tuple[tuple[str, str | None], ...] = ()
    if uri is None:
        reason = "URI выбранной версии не определён" if key is not None else "Маршрут не выбран"
    elif len(matches) > 1:
        reason = f"Неоднозначный пакет пространства «{uri}»"
    elif not matches:
        reason = f"Пакет пространства «{uri}» не найден"
    elif isinstance(value, SchemaUnavailable):
        reason = value.reason
    elif isinstance(value, EdSchema):
        imports = _closure_imports(value)
    return SideSelection(
        name,
        info.path if info is not None else None,
        key,
        uri,
        package.metadata_name if package is not None else None,
        package.description_path if package is not None else None,
        imports,
        reason,
    )


def _closure_imports(schema: EdSchema) -> tuple[tuple[str, str | None], ...]:
    by_namespace = {package.namespace: package for package in schema.packages}
    found: dict[str, str | None] = {}
    for package in schema.packages:
        for item in package.imports:
            if item.namespace in found:
                continue
            dependency = by_namespace.get(item.namespace)
            path = None
            if dependency is not None and item.status == "resolved" and dependency.sources:
                path = dependency.sources[0].path
            found[item.namespace] = path
    return tuple(sorted(found.items()))


def _build_report(
    rows: list[_IssueRow], skips: list[tuple[int, int, str, str]]
) -> ValidationReport:
    report = ValidationReport()
    seen_issues: set[tuple[str, str, str]] = set()
    for row in sorted(
        rows, key=lambda item: (_CHECK_INDEX[item.check], item.side, item.uri, item.address)
    ):
        key = (row.check, row.address, row.message)
        if key in seen_issues:
            continue
        seen_issues.add(key)
        report.issues.append(Issue(row.level, row.check, row.address, row.message))
    seen_skips: set[tuple[str, str]] = set()
    for _order, _side, check, reason in sorted(skips):
        if (check, reason) in seen_skips:
            continue
        seen_skips.add((check, reason))
        report.skipped.append(Skipped(check, reason))
    return report
