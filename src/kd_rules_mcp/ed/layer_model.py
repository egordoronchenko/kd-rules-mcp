"""Неизменяемая модель слоя расширений менеджера EnterpriseData.

Базовый ``EdDocument`` сюда не копируется и не переписывается. Действующие правила —
отдельные ревизии с происхождением. Непонятый фрагмент остаётся пропуском, а не
исключением и не молчаливым «как в базе».
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .model import (
    Coverage,
    DispatcherCase,
    EdDocument,
    Entity,
    Expr,
    Field,
    Guard,
    HandlerBinding,
    ObjectRule,
    Parameter,
    PredefinedRule,
    ProcessingRule,
    Routine,
    RuleUse,
    SourceFile,
    SourceSpan,
)


class HookKind(StrEnum):
    BEFORE = "before"
    AFTER = "after"
    AROUND = "around"
    CHANGE_CONTROL = "change_control"


class Continuation(StrEnum):
    ONCE = "once"
    NONE = "none"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class Applicability(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class OperationKind(StrEnum):
    ADD = "add"
    SET = "set"
    DELETE = "delete"
    APPEND = "append"
    MAP_INSERT = "map_insert"
    DISPATCH = "dispatch"
    INIT_EXTENSION = "init_extension"
    UNKNOWN = "unknown"


class EntityState(StrEnum):
    BASE = "base"
    ADDED = "added"
    CHANGED = "changed"
    DELETED = "deleted"


class Certainty(StrEnum):
    KNOWN = "known"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class LayerStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


# Все причины остаются данными: степень определённости определяется областью, не этим списком.
SKIP_REASONS = frozenset(
    {
        "ambiguous_manager",
        "arbitrary_replacement",
        "before_filler",
        "bsl_syntax",
        "call_not_found",
        "change_control",
        "computed_name",
        "computed_receiver",
        "computed_value",
        "collection_rebind",
        "conditional_operation",
        "condition_limit",
        "conditional_call",
        "duplicate_hook",
        "dynamic_code",
        "event_signature",
        "filler_error",
        "goto",
        "helper_unverified",
        "unmodeled_hook",
        "unsupported_event",
        "identity_mismatch",
        "incomplete_rule",
        "loop",
        "manager_signature",
        "manager_unreadable",
        "metadata_xml",
        "missing_configuration",
        "missing_metadata",
        "missing_module",
        "missing_target",
        "module_variable_escape",
        "name_mutation",
        "nonliteral_property",
        "opaque_condition",
        "opaque_dispatch",
        "opaque_exception",
        "opaque_flow",
        "opaque_return",
        "previous_handler_mismatch",
        "recursion",
        "resource_limit",
        "tainted_collection",
        "unknown_call",
        "unknown_field",
        "unknown_find",
        "unknown_property_parent",
        "unknown_statement",
        "unrecognized_annotation",
        "unresolved_delete",
        "unsupported_property_arguments",
        "v1_group",
    }
)


@dataclass(frozen=True, slots=True)
class LayerDescriptor:
    """Один корень выгрузки. Имя конфигурации не задаёт порядок: его задаёт ordinal."""

    id: str
    ordinal: int
    name: str
    root: str
    configuration_uuid: str | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Origin:
    """Место операции: файл выгрузки, процедура и цепочка вызовов."""

    layer_id: str
    metadata_kind: str
    metadata_name: str
    file_id: str
    span: SourceSpan
    procedure: str | None
    call_chain: tuple[SourceSpan, ...] = ()
    hook_id: str | None = None
    path: str = ""


@dataclass(frozen=True, slots=True)
class Footprint:
    """Область влияния непонятой операции: поле, сущность, коллекция, заполнитель или менеджер."""

    scope: str
    ref: str
    field_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Pred:
    """Предикат ограниченной грамматики. ``opaque`` не получает истинность."""

    op: str
    kind: str = ""
    arg: str = ""
    kids: tuple[Pred, ...] = ()

    @staticmethod
    def atom(kind: str, arg: str = "") -> Pred:
        return Pred("atom", kind, arg)

    @staticmethod
    def opaque() -> Pred:
        return Pred("opaque")


@dataclass(frozen=True, slots=True)
class Hook:
    id: str
    kind: str
    target_name: str
    routine: Routine
    target_origin: Origin | None
    continuation: str
    applicability: str
    origin: Origin
    target_class: str = "modeled"


@dataclass(frozen=True, slots=True)
class HandlerBodyChange:
    """Достоверный факт перехвата тела, без утверждений о его исполнении."""

    kind: str
    target_name: str
    origin: Origin


@dataclass(frozen=True, slots=True, kw_only=True)
class LayerHandlerBinding(HandlerBinding):
    body_changes: tuple[HandlerBodyChange, ...] = ()

    @property
    def body_modified(self) -> bool:
        return bool(self.body_changes)


HANDLER_EXECUTION_NOTE = (
    "Показаны статический состав правил после заполнения и связи событий с процедурами; "
    "действия кода обработчиков во время обмена не определяются."
)


@dataclass(frozen=True, slots=True)
class LayerOperation:
    id: str
    kind: str
    target_ref: str
    field_path: tuple[str, ...]
    value: Expr | Entity | None
    origin: Origin
    guards: tuple[Guard, ...]
    footprint: Footprint | None
    resolution: str
    preds: tuple[Pred, ...] = ()
    hook_id: str | None = None


@dataclass(frozen=True, slots=True)
class FieldChange:
    path: tuple[str, ...]
    before: Field[object] | tuple[object, ...] | None
    after: Field[object] | tuple[object, ...] | None
    origin: Origin
    operation_id: str


@dataclass(frozen=True, slots=True)
class EntityVersion:
    logical_id: str
    revision_id: str
    payload: Entity | None
    state: str
    changes: tuple[FieldChange, ...]
    history: tuple[str, ...]
    origins: tuple[Origin, ...]
    certainty: str
    collection: str = ""
    layer_id: str = "base"
    direction: str = ""
    headers_only: bool = False


@dataclass(frozen=True, slots=True)
class DispatchLink:
    """Одно звено цепочки: перехват или ветка диспетчера."""

    hook: Hook | None
    case: DispatcherCase | None
    layer_id: str
    continues: bool


@dataclass(frozen=True, slots=True)
class DispatchChain:
    target_name: str
    kind: str
    links: tuple[DispatchLink, ...]
    resolution: str


@dataclass(frozen=True, slots=True)
class LayerSkip:
    reason: str
    origin: Origin
    affected_ids: tuple[str, ...]
    raw: str
    check_scope: tuple[str, ...]
    preds: tuple[Pred, ...] = ()
    certainty: str = Certainty.UNKNOWN
    # Условие проверяется в этом месте списка операций, до последующих изменений.
    operation_offset: int | None = None

    def __post_init__(self) -> None:
        if (
            not self.affected_ids
            or not self.check_scope
            or not all(self.affected_ids)
            or not all(self.check_scope)
        ):
            raise ValueError("Пропуск чтения требует непустую область влияния")


@dataclass(frozen=True, slots=True)
class MapEntry:
    """Запись карты версий. Перезаписанная базовая строка остаётся в истории."""

    role: str
    plan_name: str | None
    key: str
    manager_name: str | None
    state: str
    origin: Origin


@dataclass(frozen=True, slots=True)
class EffectiveContext:
    """Один контекст исполнения заполнителя: направление, заголовки и версия ключа."""

    direction: str
    headers_only: bool
    version_key: str | None
    manager_name: str
    manager_layer_id: str
    entities: tuple[EntityVersion, ...]
    addresses: tuple[str, ...]
    dispatch_chains: tuple[DispatchChain, ...]
    references: tuple[RuleUse, ...]
    taints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreviousCall:
    """Первый прямой вызов прежнего обработчика; число включает собственных помощников."""

    routine_id: str
    target_id: str
    target_name: str
    arguments: tuple[Expr, ...]
    span: SourceSpan
    rule_id: str
    rule_name: str
    event: str
    call_count: int


@dataclass(frozen=True, slots=True)
class ExtensionReading:
    """Разбор одного модуля расширения. Операции ещё не наложены на базовый документ."""

    layer_id: str
    hooks: tuple[Hook, ...]
    operations: tuple[LayerOperation, ...]
    routines: tuple[Routine, ...]
    skips: tuple[LayerSkip, ...]
    source: SourceFile
    coverage: Coverage
    previous_calls: tuple[PreviousCall, ...] = ()


@dataclass(frozen=True, slots=True)
class EffectiveDocument(EdDocument):
    """Вход прежних проверок с доказанными вызовами тел через границы слоёв."""

    previous_calls: tuple[PreviousCall, ...] = ()


@dataclass(frozen=True, slots=True)
class LayeredManager:
    """Снимок наложения. ``base`` — документ ``read_manager``, его поля не подменяются."""

    base: EdDocument
    layers: tuple[LayerDescriptor, ...]
    hooks: tuple[Hook, ...]
    operations: tuple[LayerOperation, ...]
    revisions: tuple[EntityVersion, ...]
    contexts: tuple[EffectiveContext, ...]
    map_entries: tuple[MapEntry, ...]
    source_files: tuple[SourceFile, ...]
    coverage: tuple[tuple[str, Coverage], ...]
    routines: tuple[tuple[str, Routine], ...]
    skipped: tuple[LayerSkip, ...]
    status: str
    # Для адаптера проверок: уже прочитанный выбранный менеджер и обещанные правки,
    # включая операции с ложной защитой «цель отсутствует».
    source_document: EdDocument | None = None
    readings: tuple[ExtensionReading, ...] = ()
    unselected_managers: tuple[tuple[str, str, SourceFile], ...] = ()
    executor_hooks: tuple[tuple[str, str, str, Origin], ...] = ()

    @property
    def previous_calls(self) -> tuple[PreviousCall, ...]:
        """Свидетельства обёрток с сохранением файла и диапазона вызова."""
        return tuple(call for reading in self.readings for call in reading.previous_calls)


@runtime_checkable
class EffectiveRuleView(Protocol):
    """Действующее представление одного контекста для проверок.

    Нагрузки — исходные типы сущности. ``span`` и ``raw_text`` остаются диапазоном
    первоначальной декларации и не склеивают текст слоёв. Проверка имеет право считать
    правило действующим только при ``certainty == "known"``. ``conditional`` и ``unknown``
    требуют пропуска проверки, а не ошибки отсутствия.
    """

    pko: tuple[ObjectRule, ...]
    pod: tuple[ProcessingRule, ...]
    pkpd: tuple[PredefinedRule, ...]
    parameters: tuple[Parameter, ...]
    routines: tuple[Routine, ...]
    dispatcher_cases: tuple[DispatcherCase, ...]
    direction: str
    headers_only: bool
    certainty: str
    version_key: str | None


@dataclass(frozen=True, slots=True)
class RuleView:
    """Неизменяемая проекция ``EffectiveContext``, удовлетворяющая ``EffectiveRuleView``."""

    pko: tuple[ObjectRule, ...]
    pod: tuple[ProcessingRule, ...]
    pkpd: tuple[PredefinedRule, ...]
    parameters: tuple[Parameter, ...]
    routines: tuple[Routine, ...]
    dispatcher_cases: tuple[DispatcherCase, ...]
    direction: str
    headers_only: bool
    certainty: str
    version_key: str | None


def rule_view(context: EffectiveContext) -> RuleView:
    """Собирает представление. Неизвестная сущность остаётся в снимке, но не становится known."""

    def payloads(kind: type[object]) -> tuple[object, ...]:
        return tuple(
            version.payload
            for version in context.entities
            if version.state != EntityState.DELETED and isinstance(version.payload, kind)
        )

    pko = tuple(item for item in payloads(ObjectRule) if isinstance(item, ObjectRule))
    pod = tuple(item for item in payloads(ProcessingRule) if isinstance(item, ProcessingRule))
    pkpd = tuple(item for item in payloads(PredefinedRule) if isinstance(item, PredefinedRule))
    parameters = tuple(item for item in payloads(Parameter) if isinstance(item, Parameter))
    ranks = [version.certainty for version in context.entities]
    if set(context.taints) & {
        "manager",
        "filler",
        "hook",
        "dispatcher",
        "pko",
        "pod",
        "pkpd",
        "parameters",
    } or any(rank == Certainty.UNKNOWN for rank in ranks):
        certainty = Certainty.UNKNOWN
    elif any(rank == Certainty.CONDITIONAL for rank in ranks):
        certainty = Certainty.CONDITIONAL
    else:
        certainty = Certainty.KNOWN
    cases = tuple(
        link.case
        for chain in context.dispatch_chains
        if chain.resolution == "call"
        for link in chain.links
        if link.case is not None
    )
    routines = tuple(
        link.hook.routine
        for chain in context.dispatch_chains
        for link in chain.links
        if link.hook is not None
    )
    return RuleView(
        pko,
        pod,
        pkpd,
        parameters,
        routines,
        cases,
        context.direction,
        context.headers_only,
        certainty,
        context.version_key,
    )
