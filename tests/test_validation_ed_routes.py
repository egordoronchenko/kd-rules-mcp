"""Границы проверок ed.route.*: нарушение, чистый пример и пропуски."""

from dataclasses import replace
from pathlib import Path

import pytest

from kd2_rules_mcp.ed.address import escape_segment
from kd2_rules_mcp.ed.route_model import RouteProfile
from kd2_rules_mcp.ed.routes import read_routes
from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.validation.ed_routes import (
    EXCHANGE_MESSAGE,
    RouteSelectionError,
    compare_routes,
    select_route,
)
from kd2_rules_mcp.validation.report import Level

SCHEMAS = Path(__file__).parent / "data" / "ed" / "routes" / "schemas"
MESSAGE = SCHEMAS / "message.bin"
FORMAT = SCHEMAS / "format.bin"
BASE = "urn:example:ed"
URI = f"{BASE}/1.8"
PLAN = "ПланФормата"
MANAGER = "МенеджерОбмена"
NODE = (
    "Версия конкретного узла неизвестна; при пустом значении "
    "исполнитель выбирает минимальную версию карты (XDTO:3618)."
)
EXTENSIONS = "Расширения конфигурации не учитывались; слой base не равен живой базе."


def _manager(version: str, *, declared: bool = True) -> str:
    head = ""
    if declared:
        head = (
            "Функция ВерсияФорматаМенеджераОбмена() Экспорт\n"
            f'    Возврат "{version}";\n'
            "КонецФункции\n"
        )
    return (
        head + "Процедура ЗаполнитьПравилаКонвертацииОбъектов"
        "(НаправлениеОбмена, ПравилаКонвертации) Экспорт\n"
        "    ДобавитьПКО_Товар(ПравилаКонвертации);\n"
        "КонецПроцедуры\n"
        "Процедура ДобавитьПКО_Товар(ПравилаКонвертации)\n"
        "    Правило = ОбменДаннымиXDTOСервер."
        "ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);\n"
        '    Правило.ИмяПКО = "Товар";\n'
        "    Правило.ОбъектДанных = Метаданные.Справочники.Товары;\n"
        '    Правило.ОбъектФормата = "Документ";\n'
        "КонецПроцедуры\n"
    )


def _xml(kind: str, name: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.3/common">\n'
        f'  <{kind} uuid="00000000-0000-0000-0000-0000000000aa">\n'
        "    <Properties>\n"
        f"      <Name>{name}</Name>\n"
        f"{body}"
        "    </Properties>\n"
        f"  </{kind}>\n"
        "</MetaDataObject>\n"
    )


def _package_xml(name: str, namespace: str, revision: str) -> str:
    synonym = (
        "      <Synonym><v8:item><v8:lang>ru</v8:lang>"
        f"<v8:content>{revision}</v8:content></v8:item></Synonym>\n"
        if revision
        else ""
    )
    return _xml("XDTOPackage", name, f"{synonym}      <Namespace>{namespace}</Namespace>\n")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build(
    root: Path,
    *,
    versions: tuple[tuple[str, str], ...] = (("1.8", MANAGER),),
    global_versions: tuple[tuple[str, str], ...] | None = None,
    versions_of: dict[str, str] | None = None,
    registration: str = "xml",
    packages: tuple[tuple[str, str, str], ...] | None = None,
    without_body: tuple[str, ...] = (),
    absent: tuple[str, ...] = (),
    plan_tail: str = "",
    global_tail: str = "",
    plan_name: str = PLAN,
    format_plan: bool = True,
    interface_declared: bool = True,
) -> RouteProfile:
    """Минимальная вымышленная выгрузка с одним планом формата."""
    versions_of = versions_of or {}
    global_versions = versions if global_versions is None else global_versions
    if packages is None:
        packages = (
            ("ФорматВымышленный", URI, "1.8.2"),
            ("СообщениеОбмена", EXCHANGE_MESSAGE, "заголовок"),
        )
    modules = {name for _key, name in (*versions, *global_versions)} - set(absent)
    if registration == "manager":
        modules.add("МенеджерРегистрации")
    declared = tuple(sorted(modules | set(without_body)))
    children = [f"      <ExchangePlan>{plan_name}</ExchangePlan>"]
    children.extend(f"      <CommonModule>{name}</CommonModule>" for name in declared)
    children.append("      <CommonModule>ОбменДаннымиПереопределяемый</CommonModule>")
    children.extend(f"      <XDTOPackage>{name}</XDTOPackage>" for name, _uri, _rev in packages)
    configuration = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
        "    <Properties><Name>ВымышленнаяКонфигурация</Name></Properties>\n"
        "    <ChildObjects>\n" + "\n".join(children) + "\n    </ChildObjects>\n"
        "  </Configuration>\n"
        "</MetaDataObject>\n"
    )
    _write(root / "Configuration.xml", configuration)
    _write(root / "ExchangePlans" / f"{plan_name}.xml", _xml("ExchangePlan", plan_name, ""))
    registration_lines = ""
    if registration == "manager":
        registration_lines = (
            "    Настройки.ПравилаРегистрацииВМенеджере = Истина;\n"
            '    Настройки.ИмяМенеджераРегистрации = "МенеджерРегистрации";\n'
        )
    inserts = "\n".join(f'    ВерсииФормата.Вставить("{key}", {name});' for key, name in versions)
    plan = (
        "Процедура ПриПолученииНастроек(Настройки) Экспорт\n"
        + ("    Настройки.ЭтоПланОбменаXDTO = Истина;\n" if format_plan else "")
        + f'    Настройки.ФорматОбмена = "{BASE}";\n'
        f"{registration_lines}"
        "    ВерсииФормата = Новый Соответствие;\n"
        f"{inserts}\n"
        "    Настройки.ВерсииФорматаОбмена = ВерсииФормата;\n"
        f"{plan_tail}"
        "КонецПроцедуры\n"
    )
    _write(root / "ExchangePlans" / plan_name / "Ext" / "ManagerModule.bsl", plan)
    global_inserts = "\n".join(
        f'    ВерсииФормата.Вставить("{key}", {name});' for key, name in global_versions
    )
    global_text = (
        "Процедура ПриПолученииДоступныхВерсийФормата(ВерсииФормата) Экспорт\n"
        f"{global_inserts}\n"
        f"{global_tail}"
        "КонецПроцедуры\n"
    )
    _write(
        root / "CommonModules" / "ОбменДаннымиПереопределяемый" / "Ext" / "Module.bsl",
        global_text,
    )
    for name in declared:
        if name in without_body:
            continue
        if name == "МенеджерРегистрации":
            body = "Процедура Зарегистрировать() Экспорт\nКонецПроцедуры\n"
        else:
            body = _manager(versions_of.get(name, "2"), declared=interface_declared)
        _write(root / "CommonModules" / name / "Ext" / "Module.bsl", body)
    for name, namespace, revision in packages:
        _write(root / "XDTOPackages" / f"{name}.xml", _package_xml(name, namespace, revision))
        source = FORMAT if namespace == URI else MESSAGE if namespace == EXCHANGE_MESSAGE else None
        if source is not None:
            _write(
                root / "XDTOPackages" / name / "Ext" / "Package.bin",
                source.read_text(encoding="utf-8"),
            )
    return read_routes(root)


def couple(tmp: Path, left: dict | None = None, right: dict | None = None):
    left_profile = build(tmp / "left", **(left or {}))
    right_profile = build(tmp / "right", **(right if right is not None else (left or {})))
    selection = select_route(left_profile, right_profile)
    return left_profile, right_profile, selection


def load_text(path: Path, text: str, locate=None):
    path.write_text(text, encoding="utf-8", newline="\n")
    finder = (
        locate if locate is not None else (lambda uri: MESSAGE if uri == EXCHANGE_MESSAGE else None)
    )
    return load_schema(path, locate_import=finder)


def base_schema():
    return load_schema(
        FORMAT, locate_import=lambda uri: MESSAGE if uri == EXCHANGE_MESSAGE else None
    )


def bind(selection, left_schema, right_schema=None):
    other = left_schema if right_schema is None else right_schema
    schemas = {}
    for side, uris in selection.required_uris:
        schema = left_schema if side == "left" else other
        for uri in uris:
            schemas[(side, uri)] = schema
    return schemas


def report_of(
    tmp: Path, left: dict | None = None, right: dict | None = None, schema=None, other=None
):
    _left, _right, selection = couple(tmp, left, right)
    schemas = bind(selection, schema or base_schema(), other)
    return compare_routes(_left, _right, selection, schemas), selection


def issues(report, check: str):
    return [item for item in report.issues if item.check == check]


def assert_absent(report, check: str) -> None:
    assert issues(report, check) == []
    assert check not in {item.check for item in report.skipped}


def assert_issue(report, check: str, level: str, address: str, message: str) -> None:
    found = issues(report, check)
    assert len(found) == 1
    assert found[0].level.value == level
    assert found[0].check == check
    assert found[0].address == address
    assert found[0].message == message


def test_clean_pair_has_no_route_issues(tmp_path: Path):
    result, selection = report_of(tmp_path)
    assert selection.selected_key == "1.8"
    assert selection.common_versions == ("1.8",)
    assert selection.tied_maxima == ()
    assert selection.required_uris == (("left", (URI,)), ("right", (URI,)))
    assert result.profile.status == "statically_compatible"
    assert result.profile.quality is None
    assert result.profile.negotiated_candidate == "1.8"
    assert result.profile.actual_node_version is None
    assert result.profile.empty_node_fallback == ("1.8", "1.8")
    assert result.schema_diffs == ()
    assert result.report.issues == []
    assert [(item.check, item.reason) for item in result.report.skipped] == [
        ("ed.route.node_state", NODE),
        ("ed.route.extensions", EXTENSIONS),
    ]
    again, _selection = report_of(tmp_path / "again")
    assert [
        (item.level, item.check, item.address, item.message) for item in again.report.issues
    ] == [(item.level, item.check, item.address, item.message) for item in result.report.issues]
    assert [(item.check, item.reason) for item in again.report.skipped] == [
        (item.check, item.reason) for item in result.report.skipped
    ]


def test_empty_intersection_and_shared_version(tmp_path: Path):
    bad, _selection = report_of(
        tmp_path / "bad",
        {"versions": (("1.8", MANAGER),)},
        {
            "versions": (("1.20", MANAGER),),
            "packages": (("СообщениеОбмена", EXCHANGE_MESSAGE, ""),),
        },
    )
    assert_issue(
        bad.report,
        "ed.route.empty_intersection",
        "ошибка",
        "Пара",
        "Общая версия обмена не найдена; совпадение пакетов не заменяет карту версий.",
    )
    assert bad.profile.status == "blocked"
    assert bad.profile.negotiated_candidate is None
    skipped = {item.check for item in bad.report.skipped}
    for check in (
        "ed.route.manager_missing",
        "ed.route.package_missing",
        "ed.route.import_unresolved",
        "ed.route.exchange_message_missing",
        "ed.route.schema_diff",
        "ed.route.interface_mismatch",
        "ed.route.registration_mismatch",
    ):
        assert check in skipped
    good, selection = report_of(
        tmp_path / "good",
        {"versions": (("1.8", MANAGER), ("1.20", MANAGER))},
        {"versions": (("1.8", MANAGER),)},
    )
    assert selection.selected_key == "1.8"
    assert_absent(good.report, "ed.route.empty_intersection")


def test_partial_and_conditional_maps_do_not_prove_empty_intersection(tmp_path: Path):
    tail = (
        "    Прочее = Новый Соответствие;\n"
        '    Если ПолучитьФункциональнуюОпцию("Х") Тогда\n'
        '        Прочее.Вставить("Ключ", Значение);\n'
        "    КонецЕсли;\n"
    )
    left = build(tmp_path / "partial", plan_tail=tail)
    assert left.plans[0].status == "partial"
    assert left.plans[0].effective_map() == {"1.8": MANAGER}
    right = build(tmp_path / "other", versions=(("1.20", MANAGER),))
    selection = select_route(left, right)
    result = compare_routes(left, right, selection, {})
    assert selection.selected_key is None
    assert selection.common_versions == ()
    assert issues(result.report, "ed.route.empty_intersection") == []
    assert any(item.check == "ed.route.empty_intersection" for item in result.report.skipped)
    assert any(item.check == "ed.route.selection" for item in result.report.skipped)
    overlap_right = build(tmp_path / "overlap")
    overlap = select_route(left, overlap_right)
    assert overlap.common_versions == ("1.8",)
    assert overlap.selected_key is None
    compared = compare_routes(left, overlap_right, overlap, {})
    assert issues(compared.report, "ed.route.empty_intersection") == []
    assert any(item.check == "ed.route.empty_intersection" for item in compared.report.skipped)


def test_manager_missing_absent_and_declared_without_body(tmp_path: Path):
    right = build(
        tmp_path / "missing",
        versions=(("1.8", "НетМодуля"),),
        absent=("НетМодуля",),
    )
    left = build(tmp_path / "present")
    choice = select_route(left, right)
    result = compare_routes(left, right, choice, bind(choice, base_schema()))
    source = next(
        item for item in right.plans[0].entries if item.state == "effective" and item.key == "1.8"
    )
    assert_issue(
        result.report,
        "ed.route.manager_missing",
        "ошибка",
        f"Правая/План/{PLAN}/Версия/1.8",
        (
            "Версия «1.8» ссылается на отсутствующий общий модуль «НетМодуля» "
            f"({source.source.relative_file}:{source.source.line_start})."
        ),
    )
    declared = build(
        tmp_path / "nobody",
        versions=(("1.8", "НетМодуля"),),
        without_body=("НетМодуля",),
    )
    quiet = select_route(left, declared)
    skipped = compare_routes(left, declared, quiet, bind(quiet, base_schema()))
    assert issues(skipped.report, "ed.route.manager_missing") == []
    assert any(item.check == "ed.route.manager_missing" for item in skipped.report.skipped)
    assert skipped.profile.status == "unknown"
    clean, _selection = report_of(tmp_path / "clean")
    assert_absent(clean.report, "ed.route.manager_missing")


def test_package_missing_and_revision_is_not_another_version(tmp_path: Path):
    bad, _selection = report_of(
        tmp_path / "bad",
        right={
            "packages": (
                ("ТолькоНовее", f"{BASE}/1.20", "1.20"),
                ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
            )
        },
    )
    assert_issue(
        bad.report,
        "ed.route.package_missing",
        "предупреждение",
        f"Правая/План/{PLAN}/Версия/1.8",
        (
            f"Для выбранного маршрута не найден пакет пространства «{URI}»; "
            "объектные правила могут быть исключены исполнителем."
        ),
    )
    assert bad.profile.status == "unknown"
    good, _selection = report_of(tmp_path / "good")
    assert good.profile.left.package_metadata_name == "ФорматВымышленный"
    assert_absent(good.report, "ed.route.package_missing")
    left = build(tmp_path / "revision-left")
    assert any(item.revision_label == "1.8.2" and item.namespace == URI for item in left.packages)
    revision, _selection = report_of(tmp_path / "revision")
    assert_absent(revision.report, "ed.route.newer_unregistered")


def test_newer_unregistered_limits_names(tmp_path: Path):
    packages = (("ФорматВымышленный", URI, "1.8.2"), ("СообщениеОбмена", EXCHANGE_MESSAGE, ""))
    packages += tuple((f"Пакет{index}", f"{BASE}/1.{index}", "") for index in (9, 10, 11, 12))
    bad, _selection = report_of(tmp_path / "bad", {"packages": packages}, {})
    found = issues(bad.report, "ed.route.newer_unregistered")
    assert len(found) == 1
    assert found[0].level is Level.WARNING
    assert found[0].address == f"Левая/План/{PLAN}"
    assert found[0].message == (
        "Есть более новые пакеты вне карты версий: 1.9, 1.10, 1.11 (ещё 1); "
        "автоматически они не выбираются."
    )
    good, _selection = report_of(
        tmp_path / "good",
        {
            "versions": (("1.8", MANAGER), ("1.20", MANAGER)),
            "packages": (
                ("ФорматВымышленный", URI, "1.8.2"),
                ("Пакет20", f"{BASE}/1.20", "1.20"),
                ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
            ),
        },
    )
    assert good.profile.negotiated_candidate == "1.20"
    assert_absent(good.report, "ed.route.newer_unregistered")


def test_import_unresolved_and_resolved_dependency(tmp_path: Path):
    text = FORMAT.read_text(encoding="utf-8").replace(
        '<import namespace="http://www.1c.ru/SSL/Exchange/Message"/>',
        '<import namespace="http://www.1c.ru/SSL/Exchange/Message"/>\n'
        '  <import namespace="urn:example:missing"/>',
    )
    missing = load_text(tmp_path / "missing.bin", text)
    assert missing.status == "partial"
    bad, _selection = report_of(tmp_path / "bad", other=missing)
    assert_issue(
        bad.report,
        "ed.route.import_unresolved",
        "предупреждение",
        f"Правая/План/{PLAN}/Версия/1.8",
        "Не разрешены импорты выбранной схемы: urn:example:missing; "
        "совместимость типов не установлена.",
    )
    assert issues(bad.report, "ed.route.schema_diff") == []
    assert any(item.check == "ed.route.schema_diff" for item in bad.report.skipped)
    dependency = tmp_path / "dependency.bin"
    dependency.write_text(
        '<package xmlns="http://v8.1c.ru/8.1/xdto" targetNamespace="urn:example:missing">'
        '<objectType name="Прочее"/></package>',
        encoding="utf-8",
        newline="\n",
    )
    resolved = load_text(
        tmp_path / "resolved.bin",
        text,
        locate=lambda uri: (
            MESSAGE
            if uri == EXCHANGE_MESSAGE
            else dependency
            if uri == "urn:example:missing"
            else None
        ),
    )
    good, _selection = report_of(tmp_path / "good", schema=resolved, other=resolved)
    assert_absent(good.report, "ed.route.import_unresolved")
    assert_absent(good.report, "ed.route.schema_diff")


def test_exchange_message_missing_and_other_metadata_name(tmp_path: Path):
    partial = load_schema(FORMAT)
    assert partial.status == "partial"
    left = build(
        tmp_path / "left",
        packages=(("ФорматВымышленный", URI, "1.8.2"),),
    )
    right = build(
        tmp_path / "right",
        packages=(("ФорматВымышленный", URI, "1.8.2"),),
    )
    selection = select_route(left, right)
    result = compare_routes(
        left,
        right,
        selection,
        {("left", URI): partial, ("right", URI): partial},
    )
    found = issues(result.report, "ed.route.exchange_message_missing")
    assert [item.address for item in found] == [
        f"Левая/Пакет/{escape_segment(EXCHANGE_MESSAGE)}",
        f"Правая/Пакет/{escape_segment(EXCHANGE_MESSAGE)}",
    ]
    assert {item.level.value for item in found} == {"ошибка"}
    assert {item.message for item in found} == {
        "Отсутствует пакет общего заголовка ExchangeMessage; "
        "выбранный обмен не обеспечен схемой сообщения."
    }
    assert issues(result.report, "ed.route.import_unresolved") == []
    assert any(item.check == "ed.route.schema_diff" for item in result.report.skipped)
    good, _selection = report_of(tmp_path / "good")
    present = build(tmp_path / "named")
    assert any(
        item.metadata_name == "СообщениеОбмена" and item.namespace == EXCHANGE_MESSAGE
        for item in present.packages
    )
    assert_absent(good.report, "ed.route.exchange_message_missing")


def test_schema_diff_ignores_prefix_and_reports_structure(tmp_path: Path):
    original = FORMAT.read_text(encoding="utf-8")
    prefix = original.replace('xmlns:t="urn:example:ed/1.8"', 'xmlns:d2p1="urn:example:ed/1.8"')
    prefix = prefix.replace("t:Base", "d2p1:Base")
    prefix = prefix.replace(
        '<property name="Code" type="xs:string" lowerBound="0" upperBound="1"/>',
        '<property upperBound="1" lowerBound="0" type="xs:string" name="Code"/>',
    )
    same = load_text(tmp_path / "prefix.bin", prefix)
    equal, _selection = report_of(tmp_path / "equal", other=same)
    assert_absent(equal.report, "ed.route.schema_diff")
    upper = load_text(
        tmp_path / "upper.bin",
        original.replace(
            'name="Code" type="xs:string" lowerBound="0" upperBound="1"',
            'name="Code" type="xs:string" lowerBound="0" upperBound="-1"',
        ),
    )
    differed, _selection = report_of(tmp_path / "upper", other=upper)
    assert_issue(
        differed.report,
        "ed.route.schema_diff",
        "предупреждение",
        f"Пара/Схема/{escape_segment(URI)}",
        f"Схемы пространства «{URI}» различаются: типов +0/\u2212"
        + "0/~1; смотрите страницу schema_diff.",
    )
    assert len(differed.schema_diffs) == 1
    change = next(item for item in differed.schema_diffs[0].changes if item.field == "upper")
    assert change.property_path == "Code"
    assert change.left == "1" and change.right == "unbounded"
    nillable = load_text(
        tmp_path / "nillable.bin",
        original.replace('nillable="false"', 'nillable="true"'),
    )
    nil, _selection = report_of(tmp_path / "nil", other=nillable)
    assert any(
        item.field == "nillable" and item.right == "true" for item in nil.schema_diffs[0].changes
    )
    inherited = load_text(tmp_path / "base.bin", original.replace(' base="t:Base"', ""))
    base, _selection = report_of(tmp_path / "base", other=inherited)
    assert any(item.field == "base" and item.right == "" for item in base.schema_diffs[0].changes)
    anonymous = load_text(
        tmp_path / "anon.bin",
        original.replace(
            '<property name="Value" type="xs:string"/>',
            '<property name="Value" type="xs:decimal"/>',
        ),
    )
    anon, _selection = report_of(tmp_path / "anon", other=anonymous)
    assert any("Body" in item.property_path for item in anon.schema_diffs[0].changes)
    swapped = original.replace(
        """  <objectType name="Envelope" ordered="true">
    <property name="Head" type="xs:string"/>
    <property name="Body">
      <typeDef xsi:type="ObjectType">
        <property name="Value" type="xs:string"/>
      </typeDef>
    </property>
  </objectType>""",
        """  <objectType name="Envelope" ordered="true">
    <property name="Body">
      <typeDef xsi:type="ObjectType">
        <property name="Value" type="xs:string"/>
      </typeDef>
    </property>
    <property name="Head" type="xs:string"/>
  </objectType>""",
    )
    ordered, _selection = report_of(
        tmp_path / "ordered", other=load_text(tmp_path / "ordered.bin", swapped)
    )
    assert any(item.field == "order" for item in ordered.schema_diffs[0].changes)
    unordered = original.replace(
        """  <objectType name="Bag" ordered="false">
    <property name="Beta" type="xs:string"/>
    <property name="Alpha" type="xs:string"/>
  </objectType>""",
        """  <objectType name="Bag" ordered="false">
    <property name="Alpha" type="xs:string"/>
    <property name="Beta" type="xs:string"/>
  </objectType>""",
    )
    bag, _selection = report_of(tmp_path / "bag", other=load_text(tmp_path / "bag.bin", unordered))
    assert_absent(bag.report, "ed.route.schema_diff")
    added = load_text(
        tmp_path / "added.bin",
        original.replace("</package>", '<objectType name="Extra"/></package>'),
    )
    extra, _selection = report_of(tmp_path / "added", other=added)
    assert extra.schema_diffs[0].added_types
    assert "Extra" in extra.schema_diffs[0].added_types[0]
    facets = load_text(tmp_path / "facet.bin", original.replace('length="5"', 'length="9"'))
    facet, _selection = report_of(tmp_path / "facet", other=facets)
    assert any(item.field == "facets" for item in facet.schema_diffs[0].changes)


def test_uuid_metadata_does_not_change_schema(tmp_path: Path):
    binary = FORMAT.read_text(encoding="utf-8")
    for folder, uuid in (
        ("a", "00000000-0000-0000-0000-000000000001"),
        ("b", "ffffffff-ffff-ffff-ffff-ffffffffffff"),
    ):
        _write(tmp_path / folder / "Формат" / "Ext" / "Package.bin", binary)
        _write(
            tmp_path / folder / "Формат.xml",
            _package_xml("Формат", URI, "1.8.2").replace(
                "00000000-0000-0000-0000-0000000000aa", uuid
            ),
        )
    left = load_schema(
        tmp_path / "a" / "Формат.xml",
        locate_import=lambda uri: MESSAGE if uri == EXCHANGE_MESSAGE else None,
    )
    right = load_schema(
        tmp_path / "b" / "Формат.xml",
        locate_import=lambda uri: MESSAGE if uri == EXCHANGE_MESSAGE else None,
    )
    result, _selection = report_of(tmp_path / "pair", schema=left, other=right)
    assert_absent(result.report, "ed.route.schema_diff")


def test_interface_and_registration_mismatch(tmp_path: Path):
    bad, _selection = report_of(
        tmp_path / "bad",
        right={"versions_of": {MANAGER: "3"}, "registration": "manager"},
    )
    assert_issue(
        bad.report,
        "ed.route.interface_mismatch",
        "предупреждение",
        "Пара",
        "Интерфейсы менеджеров различаются: 2/3; "
        "это не означает несовместимость формата сообщений.",
    )
    assert_issue(
        bad.report,
        "ed.route.registration_mismatch",
        "предупреждение",
        "Пара",
        "Регистрация изменений различается: xml/manager; настройте обе стороны независимо.",
    )
    same, _selection = report_of(
        tmp_path / "same",
        {"versions_of": {MANAGER: "2"}, "registration": "xml"},
        {"versions_of": {MANAGER: "2"}, "registration": "xml"},
    )
    assert_absent(same.report, "ed.route.interface_mismatch")
    assert_absent(same.report, "ed.route.registration_mismatch")
    unknown, _selection = report_of(
        tmp_path / "unknown",
        right={"versions_of": {MANAGER: "4"}},
    )
    assert issues(unknown.report, "ed.route.interface_mismatch") == []
    assert any(item.check == "ed.route.interface_mismatch" for item in unknown.report.skipped)


def test_map_sources_differ_ignores_provenance(tmp_path: Path):
    bad, _selection = report_of(
        tmp_path / "bad",
        {"global_versions": (("1.8", "МенеджерДругой"),)},
        {},
    )
    assert_issue(
        bad.report,
        "ed.route.map_sources_differ",
        "предупреждение",
        f"Левая/План/{PLAN}",
        "Карты с узлом и без узла различаются; результат зависит от контекста вызова.",
    )
    good, _selection = report_of(
        tmp_path / "good", left={"global_tail": "    // другая строка происхождения\n"}
    )
    assert_absent(good.report, "ed.route.map_sources_differ")
    folded, _selection = report_of(
        tmp_path / "case", left={"global_versions": (("1.8", MANAGER.upper()),)}
    )
    assert_absent(folded.report, "ed.route.map_sources_differ")


def test_issue_order_and_rejected_foreign_key(tmp_path: Path):
    packages = (
        ("ФорматВымышленный", URI, "1.8.2"),
        ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
        ("Пакет20", f"{BASE}/1.20", ""),
    )
    right_packages = (
        ("ФорматВымышленный", URI, "1.8.2"),
        ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
        ("Пакет21", f"{BASE}/1.21", ""),
    )
    result, selection = report_of(
        tmp_path / "order",
        {"packages": packages},
        {"packages": right_packages, "versions_of": {MANAGER: "3"}, "registration": "manager"},
    )
    assert [item.check for item in result.report.issues] == [
        "ed.route.newer_unregistered",
        "ed.route.newer_unregistered",
        "ed.route.interface_mismatch",
        "ed.route.registration_mismatch",
    ]
    assert [item.address.startswith("Левая") for item in result.report.issues[:2]] == [True, False]
    forged = replace(selection, selected_key="0.1")
    left, right, _selection = couple(tmp_path / "pair")
    with pytest.raises(ValueError, match="не согласован"):
        compare_routes(left, right, forged, bind(selection, base_schema()))


def test_tied_beta_and_unsupported_versions(tmp_path: Path):
    tied, selection = report_of(
        tmp_path / "tied",
        {"versions": (("1.20", MANAGER), ("1.20.2", MANAGER))},
    )
    assert selection.selected_key is None
    assert selection.tied_maxima == ("1.20", "1.20.2")
    assert any(item.check == "ed.route.selection" for item in tied.report.skipped)
    assert tied.profile.status == "unknown"
    betas, beta_selection = report_of(
        tmp_path / "beta",
        {"versions": (("1.8.beta", MANAGER), ("1.8", MANAGER))},
    )
    assert beta_selection.selected_key == "1.8"
    assert not any(item.check == "ed.route.selection" for item in betas.report.skipped)
    assert issues(betas.report, "ed.route.empty_intersection") == []
    both, both_selection = report_of(
        tmp_path / "betas",
        {"versions": (("1.8.beta", MANAGER), ("1.9.beta", MANAGER))},
    )
    assert both_selection.selected_key is None
    assert both_selection.tied_maxima == ()
    assert both_selection.common_versions == ("1.8.beta", "1.9.beta")
    assert any(
        "XDTO:9176" in item.reason
        for item in both.report.skipped
        if item.check == "ed.route.selection"
    )
    assert issues(both.report, "ed.route.empty_intersection") == []
    assert both.profile.status == "unknown"
    unsupported, bad_selection = report_of(
        tmp_path / "badkey",
        {"versions": (("1.8", MANAGER), ("релиз", MANAGER))},
    )
    assert bad_selection.selected_key is None
    assert any(
        "релиз" in item.reason
        for item in unsupported.report.skipped
        if item.check == "ed.route.selection"
    )


def test_several_plans_require_names():
    root = Path(__file__).parent / "data" / "ed" / "routes" / "maps-equal"
    profile = read_routes(root)
    with pytest.raises(RouteSelectionError, match="Несколько планов"):
        select_route(profile, profile)
    chosen = select_route(profile, profile, left_plan="ПланПолный", right_plan="ПланПолный")
    assert chosen.selected_key == "1.5"
    assert chosen.left_plan == "ПланПолный"
    with pytest.raises(RouteSelectionError, match="без узла"):
        select_route(profile, profile, context="without_node", left_plan="ПланПолный")
    without = select_route(profile, profile, context="without_node")
    assert without.selected_key == "1.5"
    assert without.required_uris == ()


def test_anonymous_typedef_compares_facets_and_base(tmp_path: Path):
    original = FORMAT.read_text(encoding="utf-8")
    mark = '<property name="Mark" type="xs:string" nillable="false"/>'
    typed = (
        '<property name="Mark"><typeDef xsi:type="ValueType" '
        'base="xs:string" length="3"/></property>'
    )
    left_schema = load_text(tmp_path / "left.bin", original.replace(mark, typed))
    longer = load_text(
        tmp_path / "longer.bin",
        original.replace(mark, typed).replace('length="3"', 'length="7"'),
    )
    differed, _selection = report_of(tmp_path / "facet", schema=left_schema, other=longer)
    assert differed.schema_diffs
    facet = next(item for item in differed.schema_diffs[0].changes if item.field == "facets")
    assert facet.property_path == "Mark"
    assert "3" in facet.left and "7" in facet.right
    other_base = typed.replace('base="xs:string"', 'base="xs:decimal"')
    based = load_text(tmp_path / "base.bin", original.replace(mark, other_base))
    base, _selection = report_of(tmp_path / "base", schema=left_schema, other=based)
    assert any(
        item.field == "base" and item.property_path == "Mark" and "decimal" in item.right
        for item in base.schema_diffs[0].changes
    )
    again = load_text(tmp_path / "again.bin", original.replace(mark, typed))
    same, _selection = report_of(tmp_path / "same", schema=left_schema, other=again)
    assert_absent(same.report, "ed.route.schema_diff")


def test_only_beta_common_version_is_not_selected(tmp_path: Path):
    only, selection = report_of(tmp_path / "only", {"versions": (("1.8.beta", MANAGER),)})
    assert selection.selected_key is None
    assert selection.tied_maxima == ()
    assert selection.common_versions == ("1.8.beta",)
    assert any(
        "XDTO:9176" in item.reason
        for item in only.report.skipped
        if item.check == "ed.route.selection"
    )
    assert issues(only.report, "ed.route.empty_intersection") == []
    assert only.profile.status == "unknown"
    packages = (
        ("Ф13", f"{BASE}/1.3", ""),
        ("ФБета", f"{BASE}/1.8.beta", ""),
        ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
    )
    ceiling, _selection = report_of(
        tmp_path / "ceiling",
        {"versions": (("1.8.beta", MANAGER),), "packages": packages},
    )
    assert issues(ceiling.report, "ed.route.newer_unregistered") == []
    clean, clean_selection = report_of(
        tmp_path / "clean",
        {"versions": (("1.8.beta", MANAGER), ("1.8", MANAGER))},
    )
    assert clean_selection.selected_key == "1.8"
    assert not any(item.check == "ed.route.selection" for item in clean.report.skipped)
    assert clean.profile.status == "statically_compatible"


def test_status_unknown_when_selected_checks_are_skipped(tmp_path: Path):
    left, right, _plan = couple(tmp_path / "pair")
    selection = select_route(left, right, context="without_node")
    result = compare_routes(left, right, selection, {})
    assert selection.selected_key == "1.8"
    assert result.profile.status == "unknown"
    for check in (
        "ed.route.package_missing",
        "ed.route.exchange_message_missing",
        "ed.route.schema_diff",
        "ed.route.import_unresolved",
        "ed.route.registration_mismatch",
    ):
        assert any(item.check == check for item in result.report.skipped)
    assert issues(result.report, "ed.route.package_missing") == []
    clean, _selection = report_of(tmp_path / "clean")
    assert clean.profile.status == "statically_compatible"
    assert clean.report.issues == []


def test_passed_schema_must_match_requested_uri(tmp_path: Path):
    foreign = load_text(
        tmp_path / "foreign.bin",
        FORMAT.read_text(encoding="utf-8").replace(URI, "urn:example:ed/9.9"),
    )
    assert foreign.base_namespace == "urn:example:ed/9.9"
    left, right, selection = couple(tmp_path / "pair")
    one = compare_routes(
        left, right, selection, {("left", URI): base_schema(), ("right", URI): foreign}
    )
    assert one.schema_diffs == ()
    assert issues(one.report, "ed.route.schema_diff") == []
    assert any(
        "не соответствует" in item.reason and "urn:example:ed/9.9" in item.reason
        for item in one.report.skipped
        if item.check == "ed.route.schema_diff"
    )
    assert one.profile.status == "unknown"
    both = compare_routes(left, right, selection, {("left", URI): foreign, ("right", URI): foreign})
    assert both.schema_diffs == ()
    assert both.profile.status == "unknown"
    clean, _selection = report_of(tmp_path / "clean")
    assert clean.schema_diffs == ()
    assert clean.profile.status == "statically_compatible"


def test_catalog_gap_follows_package_path(tmp_path: Path):
    left = build(tmp_path / "left")
    right_root = tmp_path / "right"
    build(right_root)
    description = right_root / "XDTOPackages" / "СообщениеОбмена.xml"
    description.write_text(
        description.read_text(encoding="utf-8").replace(
            '<?xml version="1.0" encoding="UTF-8"?>\n',
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE MetaDataObject>\n',
        ),
        encoding="utf-8",
        newline="\n",
    )
    right = read_routes(right_root)
    reading = [item for item in right.skipped if item.code == "ed.route.reading"]
    assert reading
    assert reading[0].relative_file == "XDTOPackages/СообщениеОбмена.xml"
    assert "пакет" not in reading[0].reason.casefold()
    assert "xdto" not in reading[0].reason.casefold()
    selection = select_route(left, right)
    result = compare_routes(left, right, selection, bind(selection, base_schema()))
    assert issues(result.report, "ed.route.exchange_message_missing") == []
    assert any(item.check == "ed.route.exchange_message_missing" for item in result.report.skipped)
    assert any(
        item.check == "ed.route.reading" and "XDTOPackages/СообщениеОбмена.xml:1" in item.reason
        for item in result.report.skipped
    )
    assert result.profile.status == "unknown"
    good, _selection = report_of(tmp_path / "good")
    assert_absent(good.report, "ed.route.exchange_message_missing")
    assert good.profile.status == "statically_compatible"


def test_ambiguous_package_reaches_report_with_place(tmp_path: Path):
    packages = (
        ("ФорматА", URI, ""),
        ("ФорматБ", URI, ""),
        ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
    )
    bad, _selection = report_of(tmp_path / "bad", {}, {"packages": packages})
    found = [item for item in bad.report.skipped if item.check == "ed.route.schema_ambiguous"]
    assert len(found) == 1
    assert "XDTOPackages/ФорматА.xml:1" in found[0].reason
    assert "XDTOPackages/ФорматБ.xml:1" in found[0].reason
    assert issues(bad.report, "ed.route.schema_diff") == []
    assert any(item.check == "ed.route.schema_diff" for item in bad.report.skipped)
    assert bad.profile.status == "unknown"
    other = "urn:other:ed"
    right_root = tmp_path / "other"
    build(
        right_root,
        packages=(
            ("ФорматА", f"{other}/1.8", ""),
            ("ФорматБ", f"{other}/1.8", ""),
            ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
        ),
    )
    module = right_root / "ExchangePlans" / PLAN / "Ext" / "ManagerModule.bsl"
    module.write_text(
        module.read_text(encoding="utf-8").replace(BASE, other),
        encoding="utf-8",
        newline="\n",
    )
    right = read_routes(right_root)
    left = build(tmp_path / "left")
    selection = select_route(left, right)
    assert selection.selected_key == "1.8"
    compared = compare_routes(left, right, selection, bind(selection, base_schema()))
    assert any(
        item.check == "ed.route.schema_ambiguous" and f"{other}/1.8" in item.reason
        for item in compared.report.skipped
    )
    assert any(
        item.check == "ed.route.schema_diff"
        and f"{BASE}/1.8" in item.reason
        and other in item.reason
        for item in compared.report.skipped
    )
    good, _selection = report_of(tmp_path / "good")
    assert_absent(good.report, "ed.route.schema_ambiguous")


def test_explicit_plan_beats_unknown_sibling(tmp_path: Path):
    root = tmp_path / "left"
    build(root)
    configuration = root / "Configuration.xml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "<ExchangePlan>ПланФормата</ExchangePlan>",
            "<ExchangePlan>ПланФормата</ExchangePlan>\n"
            "      <ExchangePlan>ПланБезXML</ExchangePlan>",
        ),
        encoding="utf-8",
        newline="\n",
    )
    left = read_routes(root)
    assert any(plan.plan_name == "ПланБезXML" and plan.is_ed is None for plan in left.plans)
    right = build(tmp_path / "right")
    chosen = select_route(left, right, left_plan=PLAN)
    assert chosen.left_plan == PLAN
    assert chosen.selected_key == "1.8"
    result = compare_routes(left, right, chosen, bind(chosen, base_schema()))
    assert result.profile.status == "statically_compatible"
    assert any(
        item.check == "ed.route.reading" and "ExchangePlans/ПланБезXML.xml:1" in item.reason
        for item in result.report.skipped
    )
    with pytest.raises(RouteSelectionError, match="не найден"):
        select_route(left, right, left_plan="НетТакогоПлана")
    plain = build(tmp_path / "plain", format_plan=False)
    with pytest.raises(RouteSelectionError, match="не обменивается"):
        select_route(plain, right, left_plan=PLAN)


def test_pair_without_format_plan(tmp_path: Path):
    left = build(tmp_path / "left")
    right = build(tmp_path / "right", format_plan=False)
    assert all(plan.is_ed is False for plan in right.plans)
    selection = select_route(left, right)
    result = compare_routes(left, right, selection, {})
    assert selection.selected_key is None
    assert_issue(
        result.report,
        "ed.route.empty_intersection",
        "ошибка",
        "Пара",
        "Общая версия обмена не найдена; совпадение пакетов не заменяет карту версий.",
    )
    assert result.profile.status == "blocked"
    assert any(item.check == "ed.route.schema_diff" for item in result.report.skipped)


def test_different_base_uris_skip_one_namespace(tmp_path: Path):
    left = build(tmp_path / "left")
    right_root = tmp_path / "right"
    build(right_root)
    module = right_root / "ExchangePlans" / PLAN / "Ext" / "ManagerModule.bsl"
    module.write_text(
        module.read_text(encoding="utf-8").replace(BASE, "urn:other:ed"),
        encoding="utf-8",
        newline="\n",
    )
    right = read_routes(right_root)
    selection = select_route(left, right)
    assert selection.selected_key == "1.8"
    result = compare_routes(left, right, selection, bind(selection, base_schema()))
    assert issues(result.report, "ed.route.schema_diff") == []
    assert result.schema_diffs == ()
    assert any(
        item.check == "ed.route.schema_diff"
        and f"{BASE}/1.8" in item.reason
        and "urn:other:ed/1.8" in item.reason
        for item in result.report.skipped
    )
    assert result.profile.status == "unknown"


def test_duplicate_property_upper_bound_does_not_crash(tmp_path: Path):
    text = FORMAT.read_text(encoding="utf-8").replace(
        '<property name="Alpha" type="xs:string"/>',
        '<property name="Alpha" type="xs:string"/>\n'
        '    <property name="Alpha" type="xs:string" upperBound="-1"/>',
    )
    schema = load_text(tmp_path / "dup.bin", text)
    result, _selection = report_of(tmp_path / "pair", schema=schema, other=schema)
    assert_absent(result.report, "ed.route.schema_diff")


def test_fallback_interface_is_a_known_version(tmp_path: Path):
    origin = build(tmp_path / "origin", interface_declared=False)
    info = next(item for item in origin.managers if item.name == MANAGER)
    assert info.interface_origin == "fallback"
    assert info.interface_version == 1
    bad, _selection = report_of(tmp_path / "bad", {}, {"interface_declared": False})
    assert_issue(
        bad.report,
        "ed.route.interface_mismatch",
        "предупреждение",
        "Пара",
        "Интерфейсы менеджеров различаются: 2/1; "
        "это не означает несовместимость формата сообщений.",
    )
    good, _selection = report_of(
        tmp_path / "good",
        {"interface_declared": False},
        {"interface_declared": False},
    )
    assert_absent(good.report, "ed.route.interface_mismatch")
    assert good.profile.status == "statically_compatible"


def test_without_node_minimum_follows_reader(tmp_path: Path):
    versions = (("1.20", MANAGER), ("1.3", MANAGER), ("1.8", MANAGER))
    left = build(tmp_path / "left", versions=versions)
    right = build(tmp_path / "right", versions=versions)
    assert left.plans[0].empty_node_fallback == "1.3"
    selection = select_route(left, right, context="without_node")
    result = compare_routes(left, right, selection, {})
    assert result.profile.empty_node_fallback == ("1.3", "1.3")
    assert result.profile.status == "unknown"
    tail = (
        "    Прочее = Новый Соответствие;\n"
        '    Если ПолучитьФункциональнуюОпцию("Х") Тогда\n'
        '        Прочее.Вставить("Ключ", Значение);\n'
        "    КонецЕсли;\n"
    )
    partial = build(tmp_path / "partial", versions=versions, global_tail=tail)
    assert partial.without_node_status == "partial"
    assert "1.3" in partial.effective_without_node()
    choice = select_route(partial, right, context="without_node")
    compared = compare_routes(partial, right, choice, {})
    assert compared.profile.empty_node_fallback == (None, "1.3")
    assert any(
        "не подтверждён" in item.reason and ".bsl:" in item.reason
        for item in compared.report.skipped
        if item.check == "ed.route.selection"
    )


def _clone_plans(root: Path, names: tuple[str, ...]) -> None:
    configuration = root / "Configuration.xml"
    text = configuration.read_text(encoding="utf-8")
    block = "\n".join(f"      <ExchangePlan>{name}</ExchangePlan>" for name in names)
    configuration.write_text(
        text.replace(
            f"<ExchangePlan>{PLAN}</ExchangePlan>",
            f"<ExchangePlan>{PLAN}</ExchangePlan>\n{block}",
        ),
        encoding="utf-8",
        newline="\n",
    )
    xml = (root / "ExchangePlans" / f"{PLAN}.xml").read_text(encoding="utf-8")
    module = root / "ExchangePlans" / PLAN / "Ext" / "ManagerModule.bsl"
    body = module.read_text(encoding="utf-8")
    for name in names:
        (root / "ExchangePlans" / f"{name}.xml").write_text(
            xml.replace(PLAN, name), encoding="utf-8", newline="\n"
        )
        folder = root / "ExchangePlans" / name / "Ext"
        folder.mkdir(parents=True)
        (folder / "ManagerModule.bsl").write_text(body, encoding="utf-8", newline="\n")


def _schema_importing(tmp: Path, namespace: str):
    tmp.mkdir(parents=True, exist_ok=True)
    dependency = tmp / "dependency.bin"
    dependency.write_text(
        '<package xmlns="http://v8.1c.ru/8.1/xdto" '
        f'targetNamespace="{namespace}"><objectType name="Dep"/></package>',
        encoding="utf-8",
        newline="\n",
    )
    text = FORMAT.read_text(encoding="utf-8").replace(
        '<import namespace="http://www.1c.ru/SSL/Exchange/Message"/>',
        '<import namespace="http://www.1c.ru/SSL/Exchange/Message"/>\n'
        f'  <import namespace="{namespace}"/>',
    )

    def locate(uri: str):
        if uri == EXCHANGE_MESSAGE:
            return MESSAGE
        if uri == namespace:
            return dependency
        return None

    return load_text(tmp / "format.bin", text, locate)


def test_ambiguous_header_is_not_compatible(tmp_path: Path):
    packages = (
        ("ФорматВымышленный", URI, "1.8.2"),
        ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
        ("СообщениеОбмена2", EXCHANGE_MESSAGE, ""),
    )
    left = build(tmp_path / "left")
    right = build(tmp_path / "right", packages=packages)
    selection = select_route(left, right)
    result = compare_routes(left, right, selection, bind(selection, base_schema()))
    assert result.profile.status == "unknown"
    assert issues(result.report, "ed.route.exchange_message_missing") == []
    assert any(
        item.check == "ed.route.exchange_message_missing" and "проверка заголовка" in item.reason
        for item in result.report.skipped
    )
    assert any(item.check == "ed.route.schema_diff" for item in result.report.skipped)
    good, _selection = report_of(tmp_path / "good")
    assert good.profile.status == "statically_compatible"
    assert_absent(good.report, "ed.route.exchange_message_missing")


def test_ambiguous_import_dependency_blocks_schema(tmp_path: Path):
    dependency = "urn:dep"
    schema = _schema_importing(tmp_path / "schema", dependency)
    assert schema.status == "complete"
    single = (
        ("ФорматВымышленный", URI, "1.8.2"),
        ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
        ("Зависимость", dependency, ""),
    )
    doubled = (
        ("ФорматВымышленный", URI, "1.8.2"),
        ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
        ("ЗависимостьА", dependency, ""),
        ("ЗависимостьБ", dependency, ""),
    )
    left = build(tmp_path / "left", packages=single)
    right = build(tmp_path / "right", packages=doubled)
    selection = select_route(left, right)
    bad = compare_routes(left, right, selection, bind(selection, schema))
    assert bad.profile.status == "unknown"
    assert any(
        item.check == "ed.route.schema_diff" and dependency in item.reason
        for item in bad.report.skipped
    )
    assert any(
        item.check == "ed.route.schema_ambiguous" and dependency in item.reason
        for item in bad.report.skipped
    )
    assert issues(bad.report, "ed.route.exchange_message_missing") == []
    assert not any(item.check == "ed.route.exchange_message_missing" for item in bad.report.skipped)
    clean_left = build(tmp_path / "clean-left", packages=single)
    clean_right = build(tmp_path / "clean-right", packages=single)
    clean_selection = select_route(clean_left, clean_right)
    good = compare_routes(clean_left, clean_right, clean_selection, bind(clean_selection, schema))
    assert good.profile.status == "statically_compatible"
    assert_absent(good.report, "ed.route.schema_diff")


def test_unrelated_ambiguity_does_not_lower_status(tmp_path: Path):
    packages = (
        ("ФорматВымышленный", URI, "1.8.2"),
        ("СообщениеОбмена", EXCHANGE_MESSAGE, ""),
        ("ДругойА", "urn:unrelated", ""),
        ("ДругойБ", "urn:unrelated", ""),
    )
    result, _selection = report_of(tmp_path, {}, {"packages": packages})
    assert result.profile.status == "statically_compatible"
    assert_absent(result.report, "ed.route.schema_ambiguous")
    assert_absent(result.report, "ed.route.schema_diff")


def test_two_and_five_plans_do_not_report_negative_rest(tmp_path: Path):
    two = tmp_path / "two"
    build(two)
    _clone_plans(two, ("ПланДва",))
    with pytest.raises(RouteSelectionError) as two_error:
        select_route(read_routes(two), build(tmp_path / "two-right"))
    assert "ПланФормата, ПланДва" in str(two_error.value)
    assert "(ещё" not in str(two_error.value)

    five = tmp_path / "five"
    build(five)
    _clone_plans(five, ("План2", "План3", "План4", "План5"))
    with pytest.raises(RouteSelectionError) as five_error:
        select_route(read_routes(five), build(tmp_path / "five-right"))
    message = str(five_error.value)
    assert "ПланФормата, План2, План3" in message
    assert "(ещё 2)" in message
    assert "(ещё -" not in message


def test_unknown_foreign_plan_names_the_file(tmp_path: Path):
    root = tmp_path / "left"
    build(root)
    configuration = root / "Configuration.xml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "<ExchangePlan>ПланФормата</ExchangePlan>",
            "<ExchangePlan>ПланФормата</ExchangePlan>\n"
            "      <ExchangePlan>ПланБезXML</ExchangePlan>",
        ),
        encoding="utf-8",
        newline="\n",
    )
    left = read_routes(root)
    right = build(tmp_path / "right")
    selection = select_route(left, right)
    result = compare_routes(left, right, selection, bind(selection, base_schema()))
    assert result.profile.status == "unknown"
    reasons = [item.reason for item in result.report.skipped if item.check == "ed.route.selection"]
    assert len(reasons) == 1
    assert "не доказан набор планов обмена через формат" in reasons[0]
    assert "ExchangePlans/ПланБезXML.xml:1" in reasons[0]
    assert "left_plan" in reasons[0] and "right_plan" in reasons[0]
    assert "карта неполная" not in reasons[0]
    assert any(
        item.check == "ed.route.map_sources_differ"
        and "не доказан набор планов обмена через формат" in item.reason
        and "ExchangePlans/ПланБезXML.xml:1" in item.reason
        for item in result.report.skipped
    )


def test_explicit_default_form_matches_absent_attribute(tmp_path: Path):
    original = FORMAT.read_text(encoding="utf-8")
    explicit = load_text(
        tmp_path / "element.bin",
        original.replace(
            '<property name="Code" type="xs:string" lowerBound="0" upperBound="1"/>',
            '<property name="Code" type="xs:string" lowerBound="0" upperBound="1" form="element"/>',
        ),
    )
    capital = load_text(
        tmp_path / "capital.bin",
        original.replace(
            '<property name="Mark" type="xs:string" nillable="false"/>',
            '<property name="Mark" type="xs:string" nillable="false" form="Element"/>',
        ),
    )
    same, _selection = report_of(tmp_path / "same", schema=explicit, other=capital)
    assert_absent(same.report, "ed.route.schema_diff")
    assert same.profile.status == "statically_compatible"
    attribute = load_text(
        tmp_path / "attribute.bin",
        original.replace(
            '<property name="Code" type="xs:string" lowerBound="0" upperBound="1"/>',
            '<property name="Code" type="xs:string" lowerBound="0" upperBound="1" '
            'form="Attribute"/>',
        ),
    )
    differed, _selection = report_of(tmp_path / "differed", other=attribute)
    assert any(item.field == "form" for item in differed.schema_diffs[0].changes)


def test_unsupported_map_key_minimum_is_unproven(tmp_path: Path):
    versions = (("1.3", MANAGER), ("1.2.x", MANAGER))
    left = build(tmp_path / "left", versions=versions)
    right = build(tmp_path / "right", versions=versions)
    assert "1.2.x" in left.plans[0].effective_map()
    assert left.plans[0].empty_node_fallback is None
    selection = select_route(left, right, context="without_node")
    result = compare_routes(left, right, selection, {})
    assert result.profile.empty_node_fallback == (None, None)
    assert any(
        item.check == "ed.route.selection"
        and "не подтверждён" in item.reason
        and "1.2.x" in item.reason
        for item in result.report.skipped
    )


def test_plan_name_ignores_case(tmp_path: Path):
    profile = build(tmp_path / "left")
    other = build(tmp_path / "right")
    chosen = select_route(profile, other, left_plan=PLAN.lower(), right_plan=PLAN.swapcase())
    assert chosen.left_plan == PLAN
    assert chosen.right_plan == PLAN
    result = compare_routes(profile, other, chosen, bind(chosen, base_schema()))
    assert result.profile.left_plan == PLAN
    assert result.profile.right_plan == PLAN
    assert result.profile.status == "statically_compatible"
