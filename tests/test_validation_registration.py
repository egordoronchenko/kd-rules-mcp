"""Проверка правил регистрации (задача 5.3, спецификация `rules-validation`)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kd_rules_mcp.kd2.model import ExchangeRules, Node
from kd_rules_mcp.kd2.rules_io import load_registration_rules
from kd_rules_mcp.kd2.xmlstyle import KD_STYLE
from kd_rules_mcp.structures.store import StructureStore
from kd_rules_mcp.validation.registration import QUERY_PARAMETERS, _sample, check_registration
from kd_rules_mcp.validation.report import Level, ValidationReport

DUMP = Path(__file__).parent / "data" / "registration" / "dump"
RULES = Path(__file__).parent / "data" / "registration"


@pytest.fixture(scope="module")
def structure(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """Структура из минимальной выгрузки `tests/data/registration/dump`."""
    store = StructureStore(tmp_path_factory.mktemp("registration"))
    store.load_xml("registration", DUMP)
    connection = store.open("registration")
    yield connection
    connection.close()


def _issues(name: str, structure: sqlite3.Connection) -> ValidationReport:
    return check_registration(load_registration_rules(RULES / name), structure)


def _by_check(report: ValidationReport, check: str) -> list[str]:
    return [issue.message for issue in report.issues if issue.check == check]


def test_valid_rules_have_no_issues(structure: sqlite3.Connection) -> None:
    """Корректные правила: состав, реквизиты узла и вложенные свойства объекта.

    Файл также содержит отключённое и невалидное правила с несуществующим объектом
    и отбор-константу со строкой, которая не является свойством: читатель их не проверяет.
    Элемент состава «Курсы» без ПРО замечанием состава не является.
    У контрагентов в структуре авторегистрация «Разрешить» — предупреждение.
    ПВД не переданы — `registration.no_pvd` пропускается.
    """
    report = _issues("ok.xml", structure)
    assert report.errors == []
    assert [issue.check for issue in report.warnings] == ["registration.autoregistration"]
    assert "Справочник.Контрагенты" in report.warnings[0].message
    assert "Разрешить" in report.warnings[0].message
    assert report.warnings[0].address == "ПРО «000000002»"
    assert [(item.check, item.reason) for item in report.skipped] == [
        ("registration.no_pvd", "проект правил обмена не передан")
    ]


def test_missing_node_attribute(structure: sqlite3.Connection) -> None:
    """Сценарий «Отбор по несуществующему реквизиту узла»: ошибка с именем правила и реквизита."""
    report = _issues("issues.xml", structure)
    messages = _by_check(report, "registration.plan_property")
    assert "Правило «Отбор по узлу»: реквизит плана обмена «НетРеквизитаУзла» не найден" in messages
    found = [
        issue
        for issue in report.errors
        if issue.check == "registration.plan_property" and "НетРеквизитаУзла" in issue.message
    ]
    assert len(found) == 1
    assert found[0].address == "ПРО «000000013»"
    assert found[0].level is Level.ERROR


def test_plan_property_dereference_missing(structure: sqlite3.Connection) -> None:
    """Точки в реквизите узла — разыменование в запросе читателя, не одно имя поля."""
    messages = _by_check(_issues("issues.xml", structure), "registration.plan_property")
    assert (
        "Правило «Разыменование узла»: реквизит плана обмена «Контрагент.НетПоля» не найден"
        in messages
    )


def test_plan_tabular_attribute_missing(structure: sqlite3.Connection) -> None:
    """Читатель допускает `[ТабличнаяЧасть].Реквизит`; нет реквизита или самой части — ошибка."""
    messages = _by_check(_issues("issues.xml", structure), "registration.plan_property")
    assert (
        "Правило «Реквизит табличной части»: реквизит плана обмена "
        "«[Организации].НетРеквизитаТЧ» не найден" in messages
    )
    assert (
        "Правило «Нет табличной части»: реквизит плана обмена "
        "«[НетТабличнойЧасти].Организация» не найден" in messages
    )


def test_object_missing(structure: sqlite3.Connection) -> None:
    report = _issues("issues.xml", structure)
    assert (
        "Правило «Удалённый документ»: объект «Документ.НетОбъекта» не найден в структуре"
        in _by_check(report, "registration.object")
    )


def test_missing_object_does_not_search_similar_names(
    structure: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Промах объекта не строит подсказки: отчёт их не содержит, а поиск по всем именам дорогой."""
    import kd_rules_mcp.validation.registration as registration_module

    registration_module._OBJECTS.clear()
    registration_module._PLANS.clear()

    def forbidden(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("проверка регистрации не ищет похожие имена")

    monkeypatch.setattr("kd_rules_mcp.structures.queries.get_close_matches", forbidden)
    report = _issues("issues.xml", structure)
    assert (
        "Правило «Удалённый документ»: объект «Документ.НетОбъекта» не найден в структуре"
        in _by_check(report, "registration.object")
    )


def test_object_not_in_plan(structure: sqlite3.Connection) -> None:
    report = _issues("issues.xml", structure)
    assert (
        "Правило «Вне состава»: объект «Документ.Расход» не входит в состав плана обмена «Обмен»"
        in _by_check(report, "registration.plan_membership")
    )


def test_settings_type_mismatch_is_warning(structure: sqlite3.Connection) -> None:
    report = _issues("issues.xml", structure)
    warnings = [issue for issue in report.warnings if issue.check == "registration.settings_type"]
    assert [issue.message for issue in warnings] == [
        "Правило «Несогласованный тип»: ОбъектНастройки «СправочникСсылка.Приход»"
        " не совпадает с типом «ДокументСсылка.Приход» объекта «Документ.Приход»"
    ]
    assert warnings[0].level is Level.WARNING
    assert warnings[0].address == "ПРО «000000012»"


def test_object_property_missing(structure: sqlite3.Connection) -> None:
    messages = _by_check(_issues("issues.xml", structure), "registration.object_property")
    assert (
        "Правило «Реквизит объекта»: свойство «НетРеквизита» объекта «Документ.Приход»"
        " не найдено (нет «НетРеквизита»)" in messages
    )


def test_nested_object_property_missing(structure: sqlite3.Connection) -> None:
    messages = _by_check(_issues("issues.xml", structure), "registration.object_property")
    assert (
        "Правило «Вложенное свойство»: свойство «Контрагент.НетПоля» объекта «Документ.Приход»"
        " не найдено (нет «НетПоля»)" in messages
    )


def test_unload_mode_attribute_missing(structure: sqlite3.Connection) -> None:
    messages = _by_check(_issues("issues.xml", structure), "registration.unload_mode")
    assert messages == [
        "Правило «Режим выгрузки»: реквизит режима выгрузки «НетРежима»"
        " не найден у плана обмена «Обмен»"
    ]


def test_disabled_and_invalid_rules_are_not_checked(structure: sqlite3.Connection) -> None:
    """Отключить=true, Валидное=false и отсутствующее Валидное читатель не загружает.

    Правило внутри отключённой группы загружается: у группы признак читатель не смотрит.
    """
    report = _issues("issues.xml", structure)
    ignored = {"ПРО «000000019»", "ПРО «000000020»", "ПРО «000000021»"}
    assert not any(issue.address in ignored for issue in report.issues)
    assert any(issue.address == "ПРО «000000031»" for issue in report.errors)


def test_plan_content_discrepancies_are_warnings(structure: sqlite3.Connection) -> None:
    """Расхождение блока СоставПланаОбмена со структурой — предупреждение.

    Константа в блоке не сверяется: MD83Exp её в состав не пишет. Отсутствие ПРО
    у элемента состава замечанием не является.
    """
    report = _issues("issues.xml", structure)
    messages = _by_check(report, "registration.plan_content")
    assert messages == [
        "Авторегистрация в блоке СоставПланаОбмена отличается от структуры (1):"
        " ДокументСсылка.Приход (true → false)",
        "В блоке СоставПланаОбмена есть типы, которых нет в составе плана обмена (1):"
        " ДокументСсылка.Расход",
        "В составе плана обмена есть типы, которых нет в блоке СоставПланаОбмена (1):"
        " РегистрСведенийЗапись.Курсы",
    ]
    assert all(
        issue.level is Level.WARNING and issue.address == "ПланОбмена «Обмен»"
        for issue in report.warnings
        if issue.check == "registration.plan_content"
    )
    assert not any("Константа" in message for message in messages)
    assert not any("ПРО" in message for message in messages)


def test_unknown_exchange_plan_skips_composition_checks(structure: sqlite3.Connection) -> None:
    """Плана нет в структуре — проверки по его составу пропускаются, объект ПРО всё же ищется."""
    report = _issues("no_plan.xml", structure)
    assert [issue.check for issue in report.errors] == ["registration.exchange_plan"]
    assert report.errors[0].message == "План обмена «НетПлана» не найден в структуре"
    assert {item.check for item in report.skipped} == {
        "registration.plan_membership",
        "registration.plan_property",
        "registration.unload_mode",
        "registration.plan_content",
        "registration.autoregistration",
        "registration.no_pvd",
    }
    assert "результат неполный" in report.summary()
    assert not any(issue.check == "registration.plan_property" for issue in report.issues)


def test_structure_not_loaded() -> None:
    """Сценарий «Структура не загружена»: skip и итог с оговоркой, без «ошибок нет» отдельно."""
    report = check_registration(load_registration_rules(RULES / "ok.xml"), None)
    assert report.errors == []
    assert report.warnings == []
    assert {item.check for item in report.skipped} == {
        "registration.exchange_plan",
        "registration.object",
        "registration.plan_membership",
        "registration.plan_property",
        "registration.object_property",
        "registration.unload_mode",
        "registration.plan_content",
        "registration.autoregistration",
        "registration.no_pvd",
    }
    assert report.summary() == (
        "Ошибок и предупреждений нет; не выполнены проверки: "
        "registration.autoregistration, registration.exchange_plan, registration.no_pvd, "
        "registration.object, registration.object_property, registration.plan_content, "
        "registration.plan_membership, registration.plan_property, registration.unload_mode "
        "— результат неполный"
    )


def _pvd(selection: str, *, disabled: bool = False) -> Node:
    node = Node.new("pvd", "Правило")
    node.attrs["Отключить"] = disabled
    if selection:
        node.values["ОбъектВыборки"] = selection
    return node


def _exchange(*rules: Node) -> ExchangeRules:
    root = Node.new("exchange_rules", "ПравилаОбмена")
    unloading = Node.new("pvd_list", "ПравилаВыгрузкиДанных")
    unloading.items.extend(rules)
    root.children["ПравилаВыгрузкиДанных"] = unloading
    return ExchangeRules(root, KD_STYLE)


def test_autoregistration_allow_warns_and_deny_does_not(structure: sqlite3.Connection) -> None:
    """«Разрешить» — правила при записи не исполняются. «Запретить» замечанием не является."""
    report = _issues("ok.xml", structure)
    warnings = [
        issue for issue in report.warnings if issue.check == "registration.autoregistration"
    ]
    assert len(warnings) == 1
    assert warnings[0].address == "ПРО «000000002»"
    assert "Справочник.Контрагенты" in warnings[0].message
    assert "Разрешить" in warnings[0].message
    document = load_registration_rules(RULES / "ok.xml")
    section = document.section("ПравилаРегистрацииОбъектов")
    section.items[:] = [
        rule
        for rule in document.rules()
        if str(rule.get("ОбъектМетаданныхИмя")) == "Документ.Приход"
    ]
    clean = check_registration(document, structure)
    assert not any(issue.check == "registration.autoregistration" for issue in clean.issues)


def test_autoregistration_skipped_without_structure() -> None:
    report = check_registration(load_registration_rules(RULES / "ok.xml"), None)
    skipped = [item for item in report.skipped if item.check == "registration.autoregistration"]
    assert len(skipped) == 1
    assert skipped[0].reason == "структура конфигурации-источника не загружена"


def test_no_pvd_warns_until_every_content_type_is_covered(structure: sqlite3.Connection) -> None:
    """Тип состава без включённого ПВД с ОбъектВыборки не выгружается.

    Пустой объект выборки и отключённое ПВД тип не покрывают. Источник ПКО не подставляется:
    читатель ПВД берёт тип только из непустого ОбъектВыборки. Тип без правила регистрации и с
    запретом авторегистрации (здесь — регистр «Курсы») не регистрируется вовсе и предупреждения
    не даёт, хотя его ПВД отключено.
    """
    rules = load_registration_rules(RULES / "ok.xml")
    partial = check_registration(
        rules,
        structure,
        exchange_rules=_exchange(
            _pvd("ДокументСсылка.Приход"),
            _pvd("РегистрСведенийЗапись.Курсы", disabled=True),
            _pvd(""),
        ),
    )
    warnings = [issue for issue in partial.warnings if issue.check == "registration.no_pvd"]
    assert [issue.address for issue in warnings] == ["ПРО «000000002»"]
    assert "СправочникСсылка.Контрагенты" in warnings[0].message
    assert "регистрирует правило регистрации" in warnings[0].message
    assert "не попадает в выборку" in warnings[0].message
    covered = check_registration(
        rules,
        structure,
        exchange_rules=_exchange(
            _pvd("ДокументСсылка.Приход"),
            _pvd("СправочникСсылка.Контрагенты"),
            _pvd("РегистрСведенийЗапись.Курсы"),
        ),
    )
    assert not any(issue.check == "registration.no_pvd" for issue in covered.issues)
    assert not any(item.check == "registration.no_pvd" for item in covered.skipped)


def test_no_pvd_skipped_without_exchange_rules(structure: sqlite3.Connection) -> None:
    report = _issues("ok.xml", structure)
    skipped = [item for item in report.skipped if item.check == "registration.no_pvd"]
    assert len(skipped) == 1
    assert skipped[0].reason == "проект правил обмена не передан"


def _query_rules(handler: str, *, attrs: str = 'Валидное="true"', plan_filter: str = "") -> bytes:
    return f"""<ПравилаРегистрации>
<ВерсияФормата>2.01</ВерсияФормата>
<ПланОбмена Имя="Обмен">Обмен</ПланОбмена>
<ПравилаРегистрацииОбъектов>
<Правило {attrs}>
<Код>1</Код>
<Наименование>Приход</Наименование>
<ОбъектНастройки>ДокументСсылка.Приход</ОбъектНастройки>
<ОбъектМетаданныхИмя>Документ.Приход</ОбъектМетаданныхИмя>
{plan_filter}
<ПриОбработке>{handler}</ПриОбработке>
</Правило>
</ПравилаРегистрацииОбъектов>
</ПравилаРегистрации>""".encode()


def _query_hits(handler: str, **kwargs: str) -> ValidationReport:
    return check_registration(load_registration_rules(_query_rules(handler, **kwargs)), None)


def test_query_parameter_binding_warns_only_on_a_literal_query() -> None:
    """Исполнитель задаёт `{План}ЭтотУзел` и `СвойствоОбъекта_` + ключ, не голое имя."""
    warned = _query_hits(
        'ТекстЗапроса = "ВЫБРАТЬ Ссылка ИЗ ПланОбмена.Обмен ГДЕ Поле = &amp;ВидыЦен";'
    )
    found = [issue for issue in warned.warnings if issue.check == QUERY_PARAMETERS]
    assert len(found) == 1
    assert found[0].address == "ПРО «1»"
    assert "ВидыЦен" in found[0].message
    assert "СвойствоОбъекта_" in found[0].message
    plan_filter = (
        "<ОтборПоСвойствамПланаОбмена><ЭлементОтбора>"
        "<ЭтоСтрокаКонстанты>false</ЭтоСтрокаКонстанты>"
        "<СвойствоОбъекта>Дата</СвойствоОбъекта>"
        "</ЭлементОтбора></ОтборПоСвойствамПланаОбмена>"
    )
    bound = (
        'ТекстЗапроса = "ВЫБРАТЬ Ссылка ИЗ ПланОбмена.Обмен ГДЕ '
        "Поле = &amp;СвойствоОбъекта_Дата И Ссылка &lt;&gt; &amp;ОбменЭтотУзел "
        'И &amp;УсловиеОтбораПоРеквизитуФлагу";'
    )
    clean = _query_hits(bound, plan_filter=plan_filter)
    assert [issue for issue in clean.warnings if issue.check == QUERY_PARAMETERS] == []
    inserted = _query_hits(
        'ПараметрыЗапроса.Вставить("ВидыЦен", 1);\n'
        'ТекстЗапроса = "ВЫБРАТЬ &amp;СвойствоОбъекта_ВидыЦен";'
    )
    assert [issue for issue in inserted.warnings if issue.check == QUERY_PARAMETERS] == []
    bare_after_insert = _query_hits(
        'ПараметрыЗапроса.Вставить("ВидыЦен", 1);\nТекстЗапроса = "ВЫБРАТЬ &amp;ВидыЦен";'
    )
    assert [issue for issue in bare_after_insert.warnings if issue.check == QUERY_PARAMETERS]
    comment = _query_hits('// ТекстЗапроса = "ВЫБРАТЬ &amp;ВидыЦен";')
    assert [issue for issue in comment.warnings if issue.check == QUERY_PARAMETERS] == []
    literal = _query_hits('Сообщить("ТекстЗапроса = ""ВЫБРАТЬ &amp;ВидыЦен""");')
    assert [issue for issue in literal.warnings if issue.check == QUERY_PARAMETERS] == []
    built = _query_hits('ТекстЗапроса = ТекстЗапроса + " И Поле = &amp;ВидыЦен";')
    assert [issue for issue in built.warnings if issue.check == QUERY_PARAMETERS] == []
    assert any(item.check == QUERY_PARAMETERS for item in built.skipped)
    disabled = _query_hits(
        'ТекстЗапроса = "ВЫБРАТЬ &amp;ВидыЦен";',
        attrs='Отключить="true" Валидное="true"',
    )
    assert [issue for issue in disabled.warnings if issue.check == QUERY_PARAMETERS] == []


def test_plan_content_sample_is_truncated() -> None:
    """Расхождения состава сводятся в одно замечание: первые 10 типов и число остальных."""
    items = [f"Тип{index:02}" for index in range(12)]
    assert _sample(items) == ", ".join(items[:10]) + " и ещё 2"
    assert _sample(items[:3]) == "Тип00, Тип01, Тип02"
