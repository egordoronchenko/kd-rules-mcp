"""Неизменяемый профиль маршрутов EnterpriseData одной выгрузки (§3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .model import SourceSpan

ConditionKind = Literal["literal_variant", "static_metadata", "opaque"]
ConditionValue = Literal["true", "false", "unknown"]
EntryState = Literal["effective", "conditional", "overwritten", "unreachable"]
InterfaceOrigin = Literal["declared", "fallback", "unknown"]
RegistrationMode = Literal["xml", "manager", "none", "unknown"]
RouteStatus = Literal["complete", "partial"]
TriState = Literal["true", "false", "unknown"]


@dataclass(frozen=True, slots=True)
class RouteSource:
    """Происхождение значения: файл выгрузки, процедура и цепочка вызовов."""

    relative_file: str
    line_start: int
    line_end: int
    procedure: str
    call_chain: tuple[SourceSpan, ...] = ()
    layer: str = "base"
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class RouteCondition:
    """Условие, при котором запись попала в карту. Ложная ветка сюда не кладётся как skipped."""

    kind: ConditionKind
    raw: str
    value: ConditionValue
    source: RouteSource


@dataclass(frozen=True, slots=True)
class RouteEntry:
    """Одна вставка в карту версий. Ключ — строка после СокрЛП, без слияния 1.20 и 1.20.2."""

    key_raw: str
    key: str
    manager_name: str | None
    source: RouteSource
    conditions: tuple[RouteCondition, ...] = ()
    state: EntryState = "effective"


@dataclass(frozen=True, slots=True)
class RouteSkip:
    """Факт, который чтение не превратило в догадку. Адрес пуст у профильных оговорок."""

    code: str
    reason: str
    relative_file: str = ""
    line: int = 0


@dataclass(frozen=True, slots=True)
class ManagerInfo:
    """Общий модуль менеджера обмена, на который ссылается карта."""

    name: str
    metadata_exists: bool
    source_exists: bool
    path: str | None
    interface_version: int | None
    interface_origin: InterfaceOrigin
    directions: tuple[str, ...]
    read_diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PackageInfo:
    """Пакет XDTO основной выгрузки. Сопоставление с записью карты — по точному Namespace."""

    metadata_name: str
    namespace: str
    description_path: str
    package_path: str | None
    revision_label: str | None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FormatExtension:
    """Пара URI → расширяемая версия из переопределяемого обработчика, не из поля плана."""

    uri: str
    version: str
    source: RouteSource
    state: EntryState = "effective"


@dataclass(frozen=True, slots=True)
class RegistrationProfile:
    """Способ регистрации изменений выбранного плана. Для плана не ED — mode none."""

    mode: RegistrationMode
    manager_name: str | None = None
    manager_path: str | None = None
    template_metadata_path: str | None = None
    template_body_path: str | None = None
    source: RouteSource | None = None
    conditions: tuple[RouteCondition, ...] = ()


@dataclass(frozen=True, slots=True)
class VariantInfo:
    """Кандидат варианта настройки. Доступность в сеансе может остаться неизвестной."""

    id: str | None
    raw_id: str
    source: RouteSource
    conditions: tuple[RouteCondition, ...] = ()
    correspondent: str | None = None
    correspondent_raw: str | None = None
    metadata_predicates: tuple[RouteCondition, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanRoute:
    """Настройки одного плана обмена. Карта живёт в поле ВерсииФорматаОбмена."""

    plan_name: str
    metadata_path: str
    is_ed: bool | None
    base_namespace: str | None
    settings_source: RouteSource | None
    entries: tuple[RouteEntry, ...]
    registration: RegistrationProfile
    variants: tuple[VariantInfo, ...]
    declared_plan_extensions: tuple[FormatExtension, ...]
    status: RouteStatus
    empty_node_fallback: str | None = None
    empty_node_tied_minima: tuple[str, ...] = ()

    def effective_map(self) -> dict[str, str | None]:
        """Ключи, исход которых чтение доказало. Условные и недостижимые сюда не входят."""
        return {item.key: item.manager_name for item in self.entries if item.state == "effective"}


@dataclass(frozen=True, slots=True)
class RouteReading:
    """Счётчики покрытия грамматики. Нулевые операции карты не означают разбор всего модуля."""

    xml_plans: int
    manager_modules: int
    ed_assignments: int
    literal_insertions: int
    effective_plan: int
    effective_without_node: int
    unreachable_insertions: int
    direct_calls_plan: int
    calls_without_node: int
    module_aliases_reached: int
    static_subsystem_checks: tuple[tuple[str, str], ...]
    variant_string_assigns: int
    variant_const_assigns: int
    base_ed_packages: int
    unparsed_map_operations: int
    ed_managers: int


@dataclass(frozen=True, slots=True)
class RouteProfile:
    """Снимок маршрутов одной основной выгрузки. Расширения конфигурации не накладываются."""

    profile_id: str
    root: str
    project: str | None
    configuration: str | None
    configuration_name: str | None
    sources_fingerprint: str
    plans: tuple[PlanRoute, ...]
    without_node_entries: tuple[RouteEntry, ...]
    format_extensions: tuple[FormatExtension, ...]
    packages: tuple[PackageInfo, ...]
    managers: tuple[ManagerInfo, ...]
    skipped: tuple[RouteSkip, ...]
    status: RouteStatus
    reading: RouteReading
    declared_modules: tuple[str, ...] = ()
    declared_subsystems: tuple[str, ...] = ()
    without_node_status: RouteStatus = "complete"
    reader_version: str = "1"

    def effective_without_node(self) -> dict[str, str | None]:
        return {
            item.key: item.manager_name
            for item in self.without_node_entries
            if item.state == "effective"
        }
