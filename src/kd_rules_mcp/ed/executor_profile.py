"""Подтверждённые возможности исполнителя; номер БСП сам по себе не является доказательством."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import EdFormatError
from .forms import EVENT_INVOCATIONS, RULE_PARAMETERS, EventInvocation
from .layer_reader import parse_annotation, read_dump
from .lexer import tokenize
from .writer_model import Evidence, ExecutorProfile, Formal, Signature, Value, digest

ReceivePath = Literal["ordinary", "object"]
EXECUTOR_MODULE = "ОбменДаннымиXDTOСервер"
MAX_EXECUTOR_BYTES = 32 * 1024 * 1024


def normalized_executor_text(text: str) -> str:
    """Убирает только BOM и различие переводов строк; содержимое и комментарии сохраняются."""
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def executor_fingerprint(text: str) -> str:
    return hashlib.sha256(normalized_executor_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutorModule:
    name: str
    sha256: str
    procedures: tuple[str, ...]
    evidence: str
    raw_sha256: str = ""


@dataclass(frozen=True, slots=True)
class RoutineContract:
    name: str
    signature: Signature
    interfaces: tuple[int, ...]
    evidence: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class RuleColumn:
    table: str
    name: str
    max_length: int | None
    evidence: str
    interfaces: tuple[int, ...] = (1, 2, 3)


@dataclass(frozen=True, slots=True)
class ExecutorEvent:
    name: str
    directions: tuple[str, ...]
    receive_paths: tuple[ReceivePath, ...]
    evidence: str
    object_condition: str = ""


@dataclass(frozen=True, slots=True)
class ExecutorCapabilities:
    profile_id: str
    modules: tuple[ExecutorModule, ...]
    interfaces: tuple[int, ...]
    contracts: tuple[RoutineContract, ...]
    columns: tuple[RuleColumn, ...]
    events: tuple[ExecutorEvent, ...]
    helper_parameters: tuple[str, ...]
    helper_evidence: str
    receive_paths: tuple[ReceivePath, ...]
    runtime_verified: tuple[tuple[int, ReceivePath], ...]
    runtime_evidence: str
    invocations: tuple[tuple[str, EventInvocation], ...] = ()
    rule_parameters: tuple[tuple[str, tuple[str, ...]], ...] = ()
    rule_evidence: str = ""

    @property
    def helper_has_namespace(self) -> bool:
        return "ПространствоИмен" in self.helper_parameters

    @property
    def revision(self) -> str:
        return digest(self)

    def contract(self, name: str, interface: int) -> RoutineContract | None:
        return next(
            (
                c
                for c in self.contracts
                if c.name.casefold() == name.casefold() and interface in c.interfaces
            ),
            None,
        )

    def name_limit(self, kind: str) -> int | None:
        name = "ИмяПКО" if kind == "pko" else "Имя"
        return next(
            (c.max_length for c in self.columns if c.table == kind and c.name == name), None
        )


@dataclass(frozen=True, slots=True)
class ProfileMismatch:
    module: str
    reason: Literal[
        "missing_module",
        "fingerprint_mismatch",
        "missing_procedure",
        "unknown_profile",
        "read_error",
        "executor_intercepted",
    ]
    expected: str = ""
    actual: str = ""
    extension: str = ""
    procedures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileDetection:
    profile: ExecutorCapabilities | None
    verified: bool
    observations: tuple[Evidence, ...]
    mismatches: tuple[ProfileMismatch, ...]
    requested_profile: str | None = None

    @property
    def code(self) -> str:
        return "executor_profile_verified" if self.verified else "executor_profile_unverified"

    def model_profile(
        self, interface: int = 2, receive_path: ReceivePath = "ordinary"
    ) -> ExecutorProfile:
        """Модель хранит выбор; подтверждение остаётся результатом текущей сверки."""
        profile = self.profile
        if profile is None:
            return ExecutorProfile(
                profile_id=self.requested_profile or "", receive_mode=receive_path
            )
        return ExecutorProfile(
            profile_id=profile.profile_id,
            receive_mode=receive_path,
        )


def _contract(
    name: str,
    parameters: tuple[str, ...],
    evidence: str,
    interfaces=(1, 2, 3),
    function=False,
    required=True,
):
    return RoutineContract(
        name,
        Signature(
            "function" if function else "procedure",
            True,
            tuple(
                Formal(p, default=Value("boolean", False) if p == "ТолькоЗаголовки" else Value())
                for p in parameters
            ),
        ),
        interfaces,
        evidence,
        required,
    )


# XDTO — ОбменДаннымиXDTOСервер; ОСКД — КонвертацияОбъектноСобытияКД (docs/checks.md).
# Объектный путь требует также КонвертацияОбъектноСервер и КонвертацияОбъектноСлужебный:
# XDTO:ПрочитатьСообщениеОбмена:7358–7384; Сервер:КонвертироватьЗачитанныеДанные:482–486.
_MODULES = (
    ExecutorModule(
        EXECUTOR_MODULE,
        "d01712521164e11a760893e77b0219af10fad82fbb0f6570753b748d13a88147",
        (
            "КоллекцияПравилКонвертации",
            "КоллекцияПравилОбработкиДанных",
            "ИнициализироватьТаблицыПравилОбменаРасширенный",
            "ПрочитатьСообщениеОбмена",
        ),
        "XDTO:КоллекцияПравилКонвертации:3250–3289; "
        "ИнициализироватьТаблицыПравилОбменаРасширенный:4763–4796",
        "c5012b46c0dc35c214592f03298dd7373b2b8dcaf4fd0c52248fb3b049a90497",
    ),
    ExecutorModule(
        "КонвертацияОбъектноСервер",
        "b51956377b204438b07c3bb6689691b90bd9a8f46b5a40f7c118787129226225",
        ("КонвертироватьЗачитанныеДанные",),
        "КонвертацияОбъектноСервер:КонвертироватьЗачитанныеДанные:482–486",
    ),
    ExecutorModule(
        "КонвертацияОбъектноСлужебный",
        "4b2fe643c99e18511da381b8b6be51fa51d912f62d411b5e133fceac8d991296",
        ("КонвертироватьЗачитанныеДанные",),
        "КонвертацияОбъектноСлужебный:3350–3359,3419–3422",
    ),
    ExecutorModule(
        "КонвертацияОбъектноСобытияКД",
        "6ce9bd49ee7eb7d725bb9b813bd3b18780edfc875b6ef6d9444deb326737d7c7",
        ("ПередЗаписьюПолученныхДанных", "ПослеКонвертацииОбъекта", "ПриКонвертацииДанныхXDTO"),
        "ОСКД:ПередЗаписьюПолученныхДанных:43–65; ПослеКонвертацииОбъекта:138–161; "
        "ПриКонвертацииДанныхXDTO:201–221",
    ),
)
_CONTRACTS = (
    _contract(
        "ВерсияФорматаМенеджераОбмена",
        (),
        "XDTO:ИнициализироватьТаблицыПравилОбменаРасширенный:4763–4774",
        function=True,
    ),
    _contract(
        "ЗаполнитьПравилаОбработкиДанных",
        ("НаправлениеОбмена", "ПравилаОбработкиДанных"),
        "XDTO:ТаблицаПравилОбработкиДанных:3917",
    ),
    _contract(
        "ЗаполнитьПравилаКонвертацииОбъектов",
        ("НаправлениеОбмена", "ПравилаКонвертации"),
        "XDTO:ЗаполнитьПравилаКонвертацииОбъектовИзМодуляОбмена:4716–4728",
        interfaces=(1, 2),
    ),
    _contract(
        "ЗаполнитьПравилаКонвертацииОбъектов",
        ("КомпонентыОбмена", "ПравилаКонвертации", "ТолькоЗаголовки"),
        "XDTO:ЗаполнитьПравилаКонвертацииОбъектовИзМодуляОбмена:4716–4728",
        interfaces=(3,),
    ),
    _contract(
        "ЗаполнитьПравилаКонвертацииПредопределенныхДанных",
        ("НаправлениеОбмена", "ПравилаКонвертации"),
        "XDTO:ЗаполнитьПравилКонвертацииПредопределенныхДанныхИзМодуляОбмена:4730–4742",
        interfaces=(1, 2),
    ),
    _contract(
        "ЗаполнитьПравилаКонвертацииПредопределенныхДанных",
        ("КомпонентыОбмена", "ПравилаКонвертации"),
        "XDTO:ЗаполнитьПравилКонвертацииПредопределенныхДанныхИзМодуляОбмена:4730–4742",
        interfaces=(3,),
    ),
    _contract(
        "ЗаполнитьПараметрыКонвертации",
        ("ПараметрыКонвертации",),
        "XDTO:СтруктураПараметровКонвертации:4798–4805",
    ),
    *(
        _contract(name, ("КомпонентыОбмена",), evidence)
        for name, evidence in (
            ("ПередКонвертацией", "XDTO:ПередКонвертацией:4978"),
            ("ПослеКонвертации", "XDTO:ПроизвестиЧтениеДанных:2192"),
            ("ПередОтложеннымЗаполнением", "XDTO:ОтложенноеЗаполнениеОбъектов:7150"),
        )
    ),
    _contract(
        "ВыполнитьПроцедуруМодуляМенеджера",
        ("ИмяПроцедуры", "Параметры"),
        "XDTO:ПриКонвертацииДанныхXDTO:8597–8609",
    ),
    _contract(
        "ВыполнитьФункциюМодуляМенеджера",
        ("ИмяФункции", "Параметры"),
        "XDTO:ВыборкаДанных:8490–8493",
        function=True,
    ),
    _contract(
        "ПередОбработкойУдаляемогоОбъекта",
        ("КомпонентыОбмена", "Объект"),
        "XDTO:ПриУдаленииОбъекта:8744–8748 (в Попытка, исключение подавляется)",
        required=False,
    ),
)

# Имена ПКО/ПОД в этом исполнителе без КвалификаторыСтроки: конечный лимит не выдумывается.
# Ограничение 100 относится к ссылке из ПКС, а 60 — к варианту идентификации.
_COLUMNS = (
    RuleColumn(
        "pko",
        "СвойстваТабличныхЧастейОбработанные",
        None,
        "XDTO:КоллекцияПравилКонвертации:3280–3286",
        interfaces=(1,),
    ),
    *(
        RuleColumn(
            "pko",
            name,
            60
            if name == "ВариантИдентификации"
            else 300
            if name
            in (
                "ТипПолученныхДанныхСтрокой",
                "ИмяТаблицыПолученныхДанных",
                "ПредставлениеТипаПолученныхДанных",
                "ПоляПредставленияОбъекта",
            )
            else None,
            "XDTO:КоллекцияПравилКонвертации:3250–3289",
        )
        for name in (
            "ИмяПКО",
            "ОбъектДанных",
            "ОбъектФормата",
            "ТипПолученныхДанныхСтрокой",
            "ИмяТаблицыПолученныхДанных",
            "ПредставлениеТипаПолученныхДанных",
            "Свойства",
            "ПоляПоиска",
            "ПоляПредставленияОбъекта",
            "РеквизитыШапкиПолученныхДанных",
            "ПриОтправкеДанных",
            "ПриКонвертацииДанныхXDTO",
            "ПриПолученииЗапросаВыгрузкиОбъекта",
            "ПриУдаленииОбъектаИБ",
            "ПередЗаписьюПолученныхДанных",
            "ПриОбработкеСсылокБлокирующихКонвертацию",
            "ПослеЗагрузкиВсехДанных",
            "ПослеКонвертацииОбъекта",
            "ПравилоДляГруппыСправочника",
            "ВариантИдентификации",
            "АлгоритмПоиска",
            "Расширения",
            "ПространствоИмен",
            "РазрешитьСоздаватьОбъектИзСтруктуры",
            "ИгнорироватьПроблемыПриЗаписи",
            "СвойстваТабличныхЧастей",
        )
    ),
    *(
        RuleColumn("pod", name, None, "XDTO:КоллекцияПравилОбработкиДанных:4744–4760")
        for name in (
            "Имя",
            "ОбъектВыборкиФормат",
            "ТипСсылкиXDTO",
            "ОбъектВыборкиМетаданные",
            "ВыборкаДанных",
            "ИмяТаблицыДляВыборки",
            "ПриОбработке",
            "ИспользуемыеПКО",
        )
    ),
    RuleColumn(
        "pod", "ОчисткаДанных", None, "XDTO:ВыгрузкаОбъектаВыборки:792–811; Template.txt:23–29"
    ),
    *(
        RuleColumn(
            "property",
            name,
            100 if name == "ПравилоКонвертацииСвойства" else None,
            "XDTO:ИнициализироватьТаблицуСвойствДляПравилаКонвертации:240–254",
        )
        for name in (
            "СвойствоКонфигурации",
            "СвойствоФормата",
            "ПравилоКонвертацииСвойства",
            "ИспользуетсяАлгоритмКонвертации",
            "ОбработкаКлючевогоСвойства",
            "ОбработкаПоисковогоСвойства",
            "ИмяТЧ",
            "ПространствоИмен",
        )
    ),
)
_EVENTS = (
    ExecutorEvent(
        "ПриОтправкеДанных", ("send",), ("ordinary", "object"), "XDTO:ПриОтправкеДанных:8549–8558"
    ),
    ExecutorEvent(
        "ПриКонвертацииДанныхXDTO",
        ("receive",),
        ("ordinary", "object"),
        "XDTO:СтруктураОбъектаXDTOВДанныеИБ:1869–1873; ОСКД:ПриКонвертацииДанныхXDTO:201–221",
    ),
    ExecutorEvent(
        "ПередЗаписьюПолученныхДанных",
        ("receive",),
        ("ordinary", "object"),
        "XDTO:СтруктураОбъектаXDTOВДанныеИБ:1960–1961; ОСКД:ПередЗаписьюПолученныхДанных:43–65",
    ),
    ExecutorEvent(
        "ПослеЗагрузкиВсехДанных",
        ("receive",),
        ("ordinary",),
        "XDTO:2172–2180,ЗапомнитьОбъектДляОтложенногоЗаполнения:7081–7091,7198–7202",
    ),
    ExecutorEvent(
        "ПослеКонвертацииОбъекта",
        ("receive",),
        ("object",),
        "ОСКД:ПослеКонвертацииОбъекта:138–161; КонвертацияОбъектноСлужебный:1928",
    ),
    ExecutorEvent(
        "ПриУдаленииОбъектаИБ",
        ("receive",),
        ("object",),
        "ОСКД:ПриУдаленииОбъектаИБ:403–417; КонвертацияОбъектноСлужебный:809–811",
    ),
    ExecutorEvent(
        "ПриОбработкеСсылокБлокирующихКонвертацию",
        ("receive",),
        ("object",),
        "ОСКД:ПриОбработкеСсылокБлокирующихКонвертацию:287–309",
    ),
    # ОСКД:253 читает другой реквизит — такое поле не считается работающим событием.
    ExecutorEvent(
        "ПриПолученииЗапросаВыгрузкиОбъекта",
        ("receive",),
        (),
        "ОСКД:ПриПолученииЗапросаВыгрузкиОбъекта:242–254",
    ),
    ExecutorEvent(
        "АлгоритмПоиска",
        ("receive",),
        ("ordinary", "object"),
        "XDTO:АлгоритмПоиска:8697–8706; СтруктураОбъектаXDTOВДанныеИБ:1892–1916; "
        "Служебный:ЗначениеСложногоСвойстваБезУникальногоИдентификатора:2284–2291; "
        "ЗначениеСложногоСвойстваВРеквизитОбъекта:2985–2996; "
        "СопоставитьОбъектПоПолямПоискаИлиАлгоритмом:2509–2544",
        "Только вложенное значение: ссылка не найдена, правило предписывает поиск по ключевым "
        "полям, внешний УИД и навигационная ссылка пусты; это не регистр, "
        "ДанныеИБ = Неопределено, идентификация "
        "ПоПолямПоиска или СначалаПоУникальномуИдентификаторуПотомПоПолямПоиска "
        "и ЕстьОбработчикАлгоритмПоиска; верхний объект алгоритм не вызывает",
    ),
)
BSP_3_1_12_XDTO = ExecutorCapabilities(
    "bsp-3.1.12-xdto",
    _MODULES,
    (1, 2, 3),
    _CONTRACTS,
    _COLUMNS,
    _EVENTS,
    (
        "РодительПКС",
        "СвойствоКонфигурации",
        "СвойствоФормата",
        "ИспользуетсяАлгоритмКонвертации",
        "ПравилоКонвертацииСвойства",
        "ПространствоИмен",
    ),
    "XDTO:ИнициализироватьТаблицуСвойствДляПравилаКонвертации:240–254; Template.txt:190–199",
    ("ordinary", "object"),
    ((2, "ordinary"),),
    "docs/plans/evals/2026-10-04-ed-writer-route-pilot.md:21–34; §11.2,7 спецификации писателя",
    # Основные вызовы и доказательства переиспользованы из forms.py; объектные — ОСКД.
    (
        *EVENT_INVOCATIONS.items(),
        (
            "ПослеКонвертацииОбъекта",
            EventInvocation(
                "procedure",
                (
                    "КомпонентыОбмена",
                    "ВнешняяСсылкаСтрокой",
                    "ДанныеИБ",
                    "ЗаписьДанныхВыполнена",
                    "ПравилоКонвертации",
                ),
                "ОСКД:ПослеКонвертацииОбъекта:148–161",
            ),
        ),
        (
            "ПриУдаленииОбъектаИБ",
            EventInvocation(
                "procedure",
                ("ДанныеИБ", "УдалитьНепосредственно", "СтандартнаяОбработка"),
                "ОСКД:ПриУдаленииОбъектаИБ:406–417",
            ),
        ),
        (
            "ПриОбработкеСсылокБлокирующихКонвертацию",
            EventInvocation(
                "procedure",
                ("КомпонентыОбмена", "ПолученныеДанные", "СсылкиБлокирующиеКонвертацию"),
                "ОСКД:ПриОбработкеСсылокБлокирующихКонвертацию:298–309",
            ),
        ),
    ),
    tuple(RULE_PARAMETERS.items()),
    "reference/kd3-cfg/DataProcessors/ВыгрузкаМодуля/Ext/ObjectModule.bsl:2251,2253,2730; "
    "XDTO:ЗаполнитьПравилаКонвертацииОбъектовИзМодуляОбмена:4716–4728",
)
PROFILES = (BSP_3_1_12_XDTO,)


def detect_profile_text(
    modules: Mapping[str, str],
    *,
    requested_profile: str | None = None,
    profiles: tuple[ExecutorCapabilities, ...] = PROFILES,
) -> ProfileDetection:
    """Точное сравнение исходников; список profiles позволяет проверять синтетические профили."""
    observations = tuple(
        Evidence(name, executor_fingerprint(text)) for name, text in sorted(modules.items())
    )
    candidates = tuple(
        p for p in profiles if requested_profile is None or p.profile_id == requested_profile
    )
    if not candidates:
        return ProfileDetection(
            None,
            False,
            observations,
            (ProfileMismatch(EXECUTOR_MODULE, "unknown_profile", requested_profile or ""),),
            requested_profile,
        )
    failures = []
    for profile in candidates:
        mismatches = []
        for module in profile.modules:
            text = modules.get(module.name)
            if text is None:
                mismatches.append(ProfileMismatch(module.name, "missing_module", module.sha256))
                continue
            actual = executor_fingerprint(text)
            if actual != module.sha256:
                mismatches.append(
                    ProfileMismatch(module.name, "fingerprint_mismatch", module.sha256, actual)
                )
            names = {
                m.casefold()
                for m in re.findall(
                    r"(?mi)^\s*(?:Процедура|Функция)\s+(\w+)\s*\(", normalized_executor_text(text)
                )
            }
            for name in module.procedures:
                if name.casefold() not in names:
                    mismatches.append(ProfileMismatch(module.name, "missing_procedure", name))
        if not mismatches:
            return ProfileDetection(profile, True, observations, (), requested_profile)
        failures.extend(mismatches)
    selected = candidates[0] if requested_profile is not None else None
    return ProfileDetection(selected, False, observations, tuple(failures), requested_profile)


def detect_profile(
    configuration: str | Path,
    extension_roots: tuple[str | Path, ...] | list[str | Path] = (),
    *,
    requested_profile: str | None = None,
) -> ProfileDetection:
    """Читает только известные общие модули выгрузки; к базе и маршрутам узлов не обращается."""
    root = Path(configuration)
    modules, failures = {}, []
    for name in sorted({m.name for p in PROFILES for m in p.modules}):
        path = root / "CommonModules" / name / "Ext/Module.bsl"
        try:
            if path.is_file():
                if path.stat().st_size > MAX_EXECUTOR_BYTES:
                    failures.append(
                        ProfileMismatch(name, "read_error", actual="Превышен лимит исходника")
                    )
                else:
                    modules[name] = path.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeError):
            failures.append(ProfileMismatch(name, "read_error", actual="Исходник не прочитан"))
    result = detect_profile_text(modules, requested_profile=requested_profile, profiles=PROFILES)
    for extension_root in extension_roots:
        extension = Path(extension_root)
        dump = read_dump(extension)
        extension_name = dump.name or extension.name
        if dump.failed:
            failures.append(
                ProfileMismatch(
                    "Расширение/" + extension_name,
                    "read_error",
                    actual="Метаданные расширения не прочитаны",
                    extension=extension_name,
                )
            )
            continue
        for name, source in modules.items():
            path = extension / "CommonModules" / name / "Ext/Module.bsl"
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > MAX_EXECUTOR_BYTES:
                    raise ValueError("Превышен лимит исходника")
                text = path.read_bytes().decode("utf-8-sig")
                base_names = {
                    p.casefold(): p
                    for p in re.findall(r"(?mi)^\s*(?:Процедура|Функция)\s+(\w+)\s*\(", source)
                }
                hooks = set()
                for token in tokenize(text):
                    if token.kind == "directive":
                        annotation = parse_annotation(token.value)
                        if annotation and annotation[1].casefold() in base_names:
                            hooks.add(base_names[annotation[1].casefold()])
                if hooks:
                    procedures = tuple(sorted(hooks))
                    failures.append(
                        ProfileMismatch(
                            name,
                            "executor_intercepted",
                            actual=f"Расширение «{extension_name}»: " + ", ".join(procedures),
                            extension=extension_name,
                            procedures=procedures,
                        )
                    )
            except (OSError, UnicodeError, ValueError, EdFormatError):
                failures.append(
                    ProfileMismatch(
                        name,
                        "read_error",
                        actual=f"Расширение «{extension_name}»: исходник не прочитан",
                        extension=extension_name,
                    )
                )
    return ProfileDetection(
        result.profile,
        result.verified and not failures,
        result.observations,
        (*result.mismatches, *failures),
        requested_profile,
    )
