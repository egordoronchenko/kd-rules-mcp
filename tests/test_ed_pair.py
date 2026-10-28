"""Парная проверка модулей: каждое расхождение — синтетический вход и чистый пример."""

from pathlib import Path

import pytest

from kd_rules_mcp.ed import build_references
from kd_rules_mcp.ed.address import build_addresses
from kd_rules_mcp.ed.schema import load_schema
from kd_rules_mcp.ed.schema.profile import ValidationProfile
from kd_rules_mcp.service import Kd2Service, Settings
from kd_rules_mcp.validation.ed_chain import ChainView
from kd_rules_mcp.validation.ed_pair import validate_pair
from tests.test_ed_profile import BASE, DATA, document

_POD = """Процедура ДобавитьПОД_Тест(ПравилаОбработкиДанных)
    ПравилоОбработки = ПравилаОбработкиДанных.Добавить();
    ПравилоОбработки.Имя = "Тест";
    ПравилоОбработки.ОбъектВыборкиФормат = "Справочник.Тест";
    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");
КонецПроцедуры"""


def module(
    properties: str = "",
    *,
    pod: bool = True,
    pod_type: str = "Справочник.Тест",
    metadata: bool = True,
    rule_type: str = "Справочник.Тест",
    identification: str | None = None,
    search: str | None = None,
    handler: str | None = None,
    extra: str = "",
) -> str:
    """Модуль из `rules.bsl`: ПОД (оба направления), ПКО «Тест» с заданными ПКС."""
    text = BASE
    lines = []
    if metadata:
        lines.append("    ПравилоОбработки.ОбъектВыборкиМетаданные = Метаданные.Справочники.Тест;")
    lines.append(f'    ПравилоОбработки.ОбъектВыборкиФормат = "{pod_type}";')
    replacement = _POD.replace(
        '    ПравилоОбработки.ОбъектВыборкиФормат = "Справочник.Тест";', "\n".join(lines)
    )
    text = text.replace(_POD, replacement)
    if not pod:
        text = text.replace("    ДобавитьПОД_Тест(ПравилаОбработкиДанных);\n", "")
    text = text.replace(
        '    ПравилоКонвертации.ОбъектФормата = "Справочник.Тест";',
        f'    ПравилоКонвертации.ОбъектФормата = "{rule_type}";',
    )
    head = []
    if identification:
        head.append(f'    ПравилоКонвертации.ВариантИдентификации = "{identification}";')
    if handler:
        head.append(f'    ПравилоКонвертации.{handler} = "Обработчик_Тест";')
    if head:
        text = text.replace(
            "    СвойстваШапки = ПравилоКонвертации.Свойства;",
            "\n".join(head) + "\n    СвойстваШапки = ПравилоКонвертации.Свойства;",
        )
    body = properties
    if search:
        body += f'\n    ПравилоКонвертации.ПоляПоиска.Добавить("{search}");'
    text = text.replace("    // <properties>", body)
    if handler and "Процедура Обработчик_Тест(" not in extra:
        # Привязка без процедуры не вызывается — обработчик должен существовать.
        extra += "\nПроцедура Обработчик_Тест(Параметры)\nКонецПроцедуры\n"
    return text + "\n" + extra


_LINKED = (
    (DATA / "validation.bin")
    .read_text(encoding="utf-8")
    .replace(
        '<property name="Номер" type="xs:string" lowerBound="0"/>',
        '<property name="Номер" type="xs:string" lowerBound="0"/>'
        '<property name="Связь" type="xs:string" lowerBound="0"/>',
    )
)


def linked_schema():
    """Схема со свойством «Связь» — ссылка объекта на объект того же типа."""
    import tempfile

    folder = Path(tempfile.mkdtemp())
    path = folder / "linked.bin"
    path.write_text(_LINKED, encoding="utf-8")
    return load_schema(path)


def view(text: str, direction: str, schema: bool | str = True) -> ChainView:
    doc = document(text)
    loaded = (
        linked_schema()
        if schema == "linked"
        else load_schema(DATA / "validation.bin")
        if schema
        else None
    )
    return ChainView.build(
        direction,
        doc,
        build_addresses(doc),
        direction,
        ValidationProfile.build(loaded, "1.2", direction),
        references=build_references(doc),
    )


def pair(source: str, target: str, schema: bool | str = True):
    return validate_pair(view(source, "send", schema), view(target, "receive", schema))


def found(result, check: str) -> list:
    return [issue for issue in result.report.issues if issue.check == check]


def skipped(result, check: str) -> list[str]:
    return [item.reason for item in result.report.skipped if item.check == check]


def test_clean_pair_has_no_findings():
    result = pair(module(), module())
    assert result.report.issues == []
    assert [row["status"] for row in result.objects] == ["exchanged"]


def test_not_accepted_without_receive_pod():
    result = pair(module(), module(pod=False))
    [issue] = found(result, "ed.pair.not_accepted")
    assert issue.level.value == "info"
    assert issue.address == "Источник/ПОД/Тест"
    assert result.objects[0]["status"] == "not_accepted"


def test_search_field_unsent():
    target = module(
        '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);',
        identification="ПоПолямПоиска",
        search="НомерДок",
    )
    result = pair(module(), target)
    [issue] = found(result, "ed.pair.search_field_unsent")
    assert issue.level.value == "предупреждение"
    assert issue.address == "Приемник/ПКО/Тест/Поиск/1"
    assert "НомерДок ← Номер" in issue.message
    clean = pair(module('    ДобавитьПКС(СвойстваШапки, "Номер", "Номер", 0);'), target)
    assert found(clean, "ed.pair.search_field_unsent") == []


def test_search_field_with_sender_handler_is_skipped():
    target = module(
        '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);',
        identification="ПоПолямПоиска",
        search="НомерДок",
    )
    result = pair(module(handler="ПриОтправкеДанных"), target)
    assert found(result, "ed.pair.search_field_unsent") == []
    assert any(
        r.startswith("handler_may_supply") for r in skipped(result, "ed.pair.search_field_unsent")
    )


def test_search_field_with_handler_literal_counts_as_sent():
    extra = (
        "Процедура Обработчик_Тест(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)\n"
        '    ДанныеXDTO.Вставить("Номер", ДанныеИБ.Номер);\n'
        "КонецПроцедуры\n"
    )
    source = module(handler="ПриОтправкеДанных", extra=extra)
    target = module(
        '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);',
        identification="ПоПолямПоиска",
        search="НомерДок",
    )
    result = pair(source, target)
    assert found(result, "ed.pair.search_field_unsent") == []
    assert result.coverage["handler_literal"] >= 1


def test_search_by_uuid_only_is_not_checked():
    target = module(
        '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);',
        identification="ПоУникальномуИдентификатору",
        search="НомерДок",
    )
    assert found(pair(module(), target), "ed.pair.search_field_unsent") == []


def test_parent_field_is_optional_for_catalog():
    target = module(identification="ПоПолямПоиска", search="Код, Родитель")
    assert found(pair(module(), target), "ed.pair.search_field_unsent") == []


def test_table_unsent():
    table = (
        '    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Товары", "Товары");\n'
        '    ДобавитьПКС(СвойстваТЧ, "Количество", "Количество", 0);'
    )
    result = pair(module(), module(table))
    [issue] = found(result, "ed.pair.table_unsent")
    assert issue.address == "Приемник/ПКО/Тест/ПКТЧ/Товары<-Товары" or "Товары" in issue.address
    assert found(pair(module(table), module(table)), "ed.pair.table_unsent") == []
    assert (
        found(pair(module(handler="ПриОтправкеДанных"), module(table)), "ed.pair.table_unsent")
        == []
    )


def test_property_cleared_on_name_mismatch():
    target = module('    ДобавитьПКС(СвойстваШапки, "Наименование", "Номер", 0);')
    [issue] = found(pair(module(), target), "ed.pair.property_cleared")
    assert issue.level.value == "предупреждение"
    assert "не равно реквизиту" in issue.message


def test_property_unsent_when_names_match():
    target = module('    ДобавитьПКС(СвойстваШапки, "Номер", "Номер", 0);')
    result = pair(module(), target)
    assert found(result, "ed.pair.property_cleared") == []
    [issue] = found(result, "ed.pair.property_unsent")
    assert issue.level.value == "info"


def test_key_property_is_cleared():
    source = module().replace('    ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);\n', "")
    [issue] = found(pair(source, module()), "ed.pair.property_cleared")
    assert "ключевое" in issue.message


def test_without_target_schema_key_unknown():
    target = module('    ДобавитьПКС(СвойстваШапки, "Номер", "Номер", 0);')
    result = pair(module(), target, schema=False)
    assert found(result, "ed.pair.property_cleared") == []
    assert found(result, "ed.pair.property_unsent")
    assert any(
        r.startswith("target_schema_unavailable")
        for r in skipped(result, "ed.pair.property_cleared")
    )


def test_property_unread():
    source = module('    ДобавитьПКС(СвойстваШапки, "Номер", "Номер", 0);')
    [issue] = found(pair(source, module()), "ed.pair.property_unread")
    assert issue.address.startswith("Источник/ПКО/Тест/ПКС/")
    handled = pair(source, module(handler="ПриКонвертацииДанныхXDTO"))
    assert found(handled, "ed.pair.property_unread") == []
    assert any(
        r.startswith("handler_may_read") for r in skipped(handled, "ed.pair.property_unread")
    )


def test_receive_pod_in_send_only_direction_is_not_accepted():
    target = module().replace(
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
        '    Если НаправлениеОбмена = "Отправка" Тогда\n'
        "        ДобавитьПОД_Тест(ПравилаОбработкиДанных);\n    КонецЕсли;",
    )
    result = pair(module(), target)
    assert result.objects[0]["status"] == "not_accepted"


def test_opaque_condition_marks_object_uncertain():
    target = module().replace(
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
        "    Если НеизвестноеУсловие() Тогда\n"
        "        ДобавитьПОД_Тест(ПравилаОбработкиДанных);\n    КонецЕсли;",
    )
    result = pair(module(), target)
    assert result.objects[0]["status"] == "exchanged"
    assert result.objects[0]["certain"] is False


def test_type_outside_schema_is_removed():
    source = module(rule_type="Справочник.НетВСхеме", pod_type="Справочник.НетВСхеме")
    result = pair(source, module())
    assert [row["status"] for row in result.objects] == ["not_sent"]


def test_second_send_pod_for_same_object_is_shadowed():
    source = module(rule_type="Справочник.Тест") + (
        "\nПроцедура ДобавитьПОД_Второй(ПравилаОбработкиДанных)\n"
        "    ПравилоОбработки = ПравилаОбработкиДанных.Добавить();\n"
        '    ПравилоОбработки.Имя = "Второй";\n'
        "    ПравилоОбработки.ОбъектВыборкиМетаданные = Метаданные.Справочники.Тест;\n"
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Нет");\n'
        "КонецПроцедуры\n"
    )
    source = source.replace(
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);\n"
        "    ДобавитьПОД_Второй(ПравилаОбработкиДанных);",
    )
    result = pair(source, module())
    assert result.objects[0]["source"]["pods"] == ["ПОД/Тест"]


def test_versions_must_match():
    doc_view = view(module(), "send")
    other = view(module(), "receive")
    other = ChainView.build(
        "x",
        other.document,
        other.index,
        "receive",
        ValidationProfile.build(other.profile.schema, "1.3", "receive"),
    )
    with pytest.raises(ValueError, match="Версии"):
        validate_pair(doc_view, other)


@pytest.fixture
def service(tmp_path):
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def opened(service, tmp_path, name, text):
    from kd_rules_mcp.ed import forms

    helpers = "\n".join(forms.helper_forms(n, 2)[0] for n in ("ДобавитьПКС", "ДобавитьПКТЧ"))
    path = tmp_path / f"{name}.bsl"
    path.write_text(text + "\n" + helpers, encoding="utf-8")
    return service.ed_open(path=str(path))["project_id"]


def test_service_pair_sections_and_versions(service, tmp_path):
    source = opened(service, tmp_path, "source", module())
    target = opened(service, tmp_path, "target", module(pod=False))
    report = service.ed_pair_validate(source, target, version="1.2")
    assert report["static"] is True and report["assumptions"]
    assert report["summary"]["objects"] == {"not_accepted": 1}
    assert report["issues"]["items"][0]["check"] == "ed.pair.not_accepted"
    objects = service.ed_pair_validate(source, target, section="objects")
    assert objects["objects"]["items"][0]["status"] == "not_accepted"
    assert objects["version"] is None
    filtered = service.ed_pair_validate(source, target, section="objects", format_object="Нет")
    assert filtered["objects"]["total"] == 0
    with pytest.raises(ValueError, match="Раздел"):
        service.ed_pair_validate(source, target, section="all")


def test_service_schema_version_conflict(service, tmp_path):
    source = opened(service, tmp_path, "source", module())
    target = opened(service, tmp_path, "target", module())
    schema = service.ed_schema_open(path=str(DATA / "validation.bin"), format_version="1.2")
    with pytest.raises(ValueError, match="не совпадает"):
        service.ed_pair_validate(
            source, target, version="1.3", source_schema_id=schema["schema_id"]
        )
    report = service.ed_pair_validate(
        source, target, source_schema_id=schema["schema_id"], target_schema_id=schema["schema_id"]
    )
    assert report["version"] == "1.2"


def test_unsent_variant_with_working_alternative_is_info():
    target = module(
        '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);',
        identification="ПоПолямПоиска",
        search="НомерДок",
    ).replace(
        '    ПравилоКонвертации.ПоляПоиска.Добавить("НомерДок");',
        '    ПравилоКонвертации.ПоляПоиска.Добавить("НомерДок");\n'
        '    ПравилоКонвертации.ПоляПоиска.Добавить("Код");',
    )
    [issue] = found(pair(module(), target), "ed.pair.search_field_unsent")
    assert issue.level.value == "info" and "остальным вариантам" in issue.message


def test_receiver_property_outside_schema_is_skipped():
    target = module('    ДобавитьПКС(СвойстваШапки, "НетТакого", "НетТакого", 0);')
    result = pair(module(), target)
    assert found(result, "ed.pair.property_unsent") == []
    assert found(result, "ed.pair.property_cleared") == []
    assert any(
        r.startswith("property_not_in_schema") for r in skipped(result, "ed.pair.property_unsent")
    )


LINK = '    ДобавитьПКС(СвойстваШапки, "Связь", "Связь", , "Тест");'
NUMBER = '    ДобавитьПКС(СвойстваШапки, "Номер", "Номер", 0);'


def second_search(text: str, first: str, second: str) -> str:
    return text.replace(
        f'    ПравилоКонвертации.ПоляПоиска.Добавить("{first}");',
        f'    ПравилоКонвертации.ПоляПоиска.Добавить("{first}");\n'
        f'    ПравилоКонвертации.ПоляПоиска.Добавить("{second}");',
    )


def other_rule(name: str, typ: str, body: str = "") -> str:
    return (
        f"\nПроцедура ДобавитьПКО_{name}(ПравилаКонвертации)\n"
        "    ПравилоКонвертации = ОбменДаннымиXDTOСервер."
        "ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);\n"
        f'    ПравилоКонвертации.ИмяПКО = "{name}";\n'
        f"    ПравилоКонвертации.ОбъектДанных = Метаданные.Справочники.{name};\n"
        f'    ПравилоКонвертации.ОбъектФормата = "{typ}";\n'
        "    СвойстваШапки = ПравилоКонвертации.Свойства;\n"
        f"{body}"
        "КонецПроцедуры\n"
    )


def with_rule(text: str, name: str, typ: str, body: str = "", use: bool = False) -> str:
    text = text.replace(
        "    ДобавитьПКО_Тест(ПравилаКонвертации);",
        f"    ДобавитьПКО_Тест(ПравилаКонвертации);\n    ДобавитьПКО_{name}(ПравилаКонвертации);",
    )
    if use:
        text = text.replace(
            '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");',
            f'    ПравилоОбработки.ИспользуемыеПКО.Добавить("{name}");',
        )
    return text + other_rule(name, typ, body)


def test_not_sent_without_pod_and_without_references():
    result = pair(module(pod=False), module())
    assert result.objects[0]["status"] == "not_sent"
    assert "ни ПОД отправки, ни ссылок" in found(result, "ed.pair.not_sent")[0].message
    other = module(rule_type="Справочник.Другой", pod=False)
    result = pair(other, module())
    assert result.objects[0]["status"] == "not_sent"
    assert "нет ПКО этого типа" in found(result, "ed.pair.not_sent")[0].message


def test_reference_only_when_sent_object_links_to_it():
    # «Тест» шлёт ссылку на «Другой»; у «Другой» нет ПОД — уходит только ссылкой.
    source = with_rule(
        module('    ДобавитьПКС(СвойстваШапки, "Связь", "Связь", , "Другой");'),
        "Другой",
        "Справочник.Другой",
    )
    target = with_rule(module(), "Другой", "Справочник.Другой")
    target = target.replace(
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);\n"
        "    ДобавитьПОД_Другой(ПравилаОбработкиДанных);",
    ) + (
        "\nПроцедура ДобавитьПОД_Другой(ПравилаОбработкиДанных)\n"
        "    ПравилоОбработки = ПравилаОбработкиДанных.Добавить();\n"
        '    ПравилоОбработки.Имя = "Другой";\n'
        '    ПравилоОбработки.ОбъектВыборкиФормат = "Справочник.Другой";\n'
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Другой");\n'
        "КонецПроцедуры\n"
    )
    result = pair(source, target, schema=False)
    status = {row["format_object"]: row["status"] for row in result.objects}
    assert status["Справочник.Другой"] == "reference_only"


def test_receive_pko_filtered_with_alternative_is_info():
    target = with_rule(module(), "Чужой", "Справочник.Чужой").replace(
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");',
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");\n'
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Чужой");',
    )
    result = pair(module(), target, schema=False)
    [issue] = found(result, "ed.pair.receive_pko_filtered")
    assert issue.level.value == "info" and issue.address == "Приемник/ПКО/Чужой"
    assert result.objects[0]["status"] == "exchanged"


def test_receive_pko_filtered_without_alternative_is_warning():
    # ПОД получения «Справочник.Тест» использует только ПКО типа «Б», которого приёмник не объявил.
    source = with_rule(module(), "Б", "Справочник.Б")
    target = with_rule(module(), "Б", "Справочник.Б", use=True)
    result = pair(source, target, schema=False)
    [issue] = found(result, "ed.pair.receive_pko_filtered")
    assert issue.level.value == "предупреждение" and "не загрузится" in issue.message
    status = {row["format_object"]: row["status"] for row in result.objects}
    assert status["Справочник.Тест"] == "not_loaded"
    assert found(result, "ed.pair.property_unread") == []


def test_search_field_not_key_when_sent_by_reference():
    source = module(NUMBER + "\n" + LINK)
    target = module(LINK + "\n" + NUMBER, identification="ПоПолямПоиска", search="Номер")
    result = pair(source, target, schema="linked")
    [issue] = found(result, "ed.pair.search_field_not_key")
    assert issue.level.value == "предупреждение" and "Номер ← Номер" in issue.message
    keyed = module(LINK, identification="ПоПолямПоиска", search="Код")
    assert found(pair(source, keyed, schema="linked"), "ed.pair.search_field_not_key") == []
    # Без ссылки у источника проверки по ссылке нет.
    plain = pair(module(NUMBER), target, schema="linked")
    assert found(plain, "ed.pair.search_field_not_key") == []


def test_reference_to_type_outside_schema_is_not_a_reference():
    source = with_rule(
        module('    ДобавитьПКС(СвойстваШапки, "Связь", "Связь", , "Д");'), "Д", "Документ.Д"
    )
    target = module(LINK + "\n" + NUMBER, identification="ПоПолямПоиска", search="Номер")
    assert found(pair(source, target, schema="linked"), "ed.pair.search_field_not_key") == []


def test_reference_from_table_row_counts():
    table = (
        '    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Товары", "Товары");\n'
        '    ДобавитьПКС(СвойстваТЧ, "Количество", "Количество", , "Тест");'
    )
    target = module(LINK + "\n" + NUMBER, identification="ПоПолямПоиска", search="Номер")
    result = pair(module(NUMBER + "\n" + table), target, schema="linked")
    assert found(result, "ed.pair.search_field_not_key")


def test_not_key_variant_with_key_alternative_is_info():
    source = module(NUMBER + "\n" + LINK)
    target = second_search(
        module(LINK + "\n" + NUMBER, identification="ПоПолямПоиска", search="Номер"),
        "Номер",
        "Код",
    )
    [issue] = found(pair(source, target, schema="linked"), "ed.pair.search_field_not_key")
    assert issue.level.value == "info"


@pytest.mark.parametrize(
    "handler", ["ПриКонвертацииДанныхXDTO", "ПередЗаписьюПолученныхДанных", "АлгоритмПоиска"]
)
def test_receiver_handlers_turn_findings_into_skips(handler):
    table = (
        '    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Товары", "Товары");\n'
        '    ДобавитьПКС(СвойстваТЧ, "Количество", "Количество", 0);'
    )
    body = '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);\n' + table
    plain = pair(module(), module(body, identification="ПоПолямПоиска", search="НомерДок"))
    assert [i.level.value for i in plain.report.issues].count("предупреждение") == 3
    handled = pair(
        module(),
        module(body, identification="ПоПолямПоиска", search="НомерДок", handler=handler),
    )
    # Перед записью идёт после поиска (XDTO:1975): поиск она не закрывает.
    expected = {"ПриКонвертацииДанныхXDTO": 0, "ПередЗаписьюПолученныхДанных": 1}.get(handler, 2)
    assert [i.level.value for i in handled.report.issues].count("предупреждение") == expected


def test_opaque_property_condition_is_skip():
    opaque = (
        '    Если ВерсияФорматаОбмена >= "1.10" Тогда\n'
        '        ДобавитьПКС(СвойстваШапки, "Наименование", "Номер", 0);\n'
        "    КонецЕсли;"
    )
    result = pair(module(), module(opaque))
    assert found(result, "ed.pair.property_cleared") == []
    sender = pair(
        module(opaque), module('    ДобавитьПКС(СвойстваШапки, "Наименование", "Номер", 0);')
    )
    assert found(sender, "ed.pair.property_cleared") == []


def test_duplicate_pko_name_uses_first_row():
    source = module() + (
        "\nПроцедура ДобавитьПКО_Тест2(ПравилаКонвертации)\n"
        "    ПравилоКонвертации = ОбменДаннымиXDTOСервер."
        "ИнициализироватьПравилоКонвертацииОбъекта(ПравилаКонвертации);\n"
        '    ПравилоКонвертации.ИмяПКО = "Тест";\n'
        "    ПравилоКонвертации.ОбъектДанных = Метаданные.Справочники.Тест;\n"
        '    ПравилоКонвертации.ОбъектФормата = "Справочник.Тест";\n'
        "    СвойстваШапки = ПравилоКонвертации.Свойства;\n"
        '    ДобавитьПКС(СвойстваШапки, "Номер", "Имя", 0);\n'
        "КонецПроцедуры\n"
    )
    source = source.replace(
        "    ДобавитьПКО_Тест(ПравилаКонвертации);",
        "    ДобавитьПКО_Тест(ПравилаКонвертации);\n    ДобавитьПКО_Тест2(ПравилаКонвертации);",
    )
    target = module('    ДобавитьПКС(СвойстваШапки, "Наименование", "Имя", 0);')
    result = pair(source, target, schema=False)
    assert found(result, "ed.pair.property_cleared")


def test_send_pod_without_metadata_is_not_a_sender():
    result = pair(module(metadata=False), module())
    assert result.objects[0]["status"] == "not_sent"
    assert any(r.startswith("pod_without_metadata") for r in skipped(result, "ed.pair"))


def test_pod_handler_makes_object_uncertain():
    source = module().replace(
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");',
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");\n'
        '    ПравилоОбработки.ПриОбработке = "ПОД_Тест_ПриОбработке";',
    )
    # Привязка без процедуры не вызывается и неопределённости не добавляет.
    assert "certain" not in pair(source, module()).objects[0]
    source += "\nПроцедура ПОД_Тест_ПриОбработке(ДанныеИБ, ИспользованиеПКО, КомпонентыОбмена)\n"
    source += "КонецПроцедуры\n"
    result = pair(source, module())
    assert result.objects[0]["certain"] is False


def test_versioned_format_type_is_resolved_by_branch():
    source = module().replace(
        '    ПравилоКонвертации.ОбъектФормата = "Справочник.Тест";',
        '    Если НаправлениеОбмена = "Отправка" Тогда\n'
        '        ПравилоКонвертации.ОбъектФормата = "Справочник.Тест";\n'
        "    Иначе\n"
        '        ПравилоКонвертации.ОбъектФормата = "Справочник.Другой";\n'
        "    КонецЕсли;",
    )
    result = pair(source, module(), schema=False)
    assert [row["status"] for row in result.objects] == ["exchanged"]


def test_search_without_any_working_variant_is_warning():
    target = second_search(
        module(
            '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);',
            identification="ПоПолямПоиска",
            search="НомерДок",
        ),
        "НомерДок",
        "БезПКС",
    )
    [issue] = found(pair(module(), target), "ed.pair.search_field_unsent")
    assert issue.level.value == "предупреждение"


def test_second_pks_of_same_field_fills_it():
    target = module(
        '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);\n'
        '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Код", 0);',
        identification="ПоПолямПоиска",
        search="НомерДок",
    )
    result = pair(module(), target)
    assert found(result, "ed.pair.search_field_unsent") == []
    assert found(result, "ed.pair.property_cleared") == []


def test_table_column_unsent():
    source_table = (
        '    СвойстваТЧ = ДобавитьПКТЧ(ПравилоКонвертации, "Товары", "Товары");\n'
        '    ДобавитьПКС(СвойстваТЧ, "Количество", "Количество", 0);'
    )
    target_table = source_table + '\n    ДобавитьПКС(СвойстваТЧ, "Цена", "Цена", 0);'
    result = pair(module(source_table), module(target_table), schema=False)
    [issue] = found(result, "ed.pair.table_column_unsent")
    assert issue.level.value == "info" and "Цена" in issue.message


def test_references_only_from_objects_in_the_intersection():
    # «Тест» ссылается на «Другой», но сам приёмником не принимается: ссылка не уйдёт.
    source = with_rule(
        module('    ДобавитьПКС(СвойстваШапки, "Связь", "Связь", , "Другой");'),
        "Другой",
        "Справочник.Другой",
    )
    target = (
        with_rule(module(), "Другой", "Справочник.Другой")
        .replace(
            '    ПравилоОбработки.ОбъектВыборкиФормат = "Справочник.Тест";',
            '    ПравилоОбработки.ОбъектВыборкиФормат = "Справочник.Другой";',
        )
        .replace(
            '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");',
            '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Другой");',
        )
    )
    status = {r["format_object"]: r["status"] for r in pair(source, target, schema=False).objects}
    assert status == {"Справочник.Тест": "not_accepted", "Справочник.Другой": "not_sent"}


def test_receive_pod_without_rules_explains_not_loaded():
    target = module().replace(
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");',
        '    ПравилоОбработки.ИспользуемыеПКО.Добавить("Нет");',
    )
    result = pair(module(), target)
    assert result.objects[0]["status"] == "not_loaded"
    [issue] = found(result, "ed.pair.receive_pko_filtered")
    assert issue.level.value == "предупреждение" and "нет ПКО «Нет»" in issue.message


def test_unknown_key_pod_earlier_makes_later_uncertain():
    dynamic = (
        "\nПроцедура ДобавитьПОД_Дин(ПравилаОбработкиДанных)\n"
        "    ПравилоОбработки = ПравилаОбработкиДанных.Добавить();\n"
        '    ПравилоОбработки.Имя = "Дин";\n'
        "    ПравилоОбработки.ОбъектВыборкиМетаданные = ОбъектПоИмени();\n"
        "КонецПроцедуры\n"
    )
    source = (module() + dynamic).replace(
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
        "    ДобавитьПОД_Дин(ПравилаОбработкиДанных);\n"
        "    ДобавитьПОД_Тест(ПравилаОбработкиДанных);",
    )
    result = pair(source, module())
    assert result.objects[0]["certain"] is False
    assert "shadowed_maybe" in result.objects[0]["source"]["reasons"]


def test_receive_pod_stub_without_rules_is_info():
    target = module().replace('    ПравилоОбработки.ИспользуемыеПКО.Добавить("Тест");\n', "")
    result = pair(module(), target)
    assert result.objects[0]["status"] == "not_loaded"
    [issue] = found(result, "ed.pair.receive_pko_filtered")
    assert issue.level.value == "info" and "заглушка" in issue.message


def test_is_group_field_is_filled_by_executor():
    # ЭтоГруппа ставит исполнитель; булево всегда заполнено (живая проба 28.10.2026).
    target = module(
        '    ДобавитьПКС(СвойстваШапки, "НомерДок", "Номер", 0);',
        identification="ПоПолямПоиска",
        search="НомерДок, ЭтоГруппа",
    )
    [issue] = found(pair(module(), target), "ed.pair.search_field_unsent")
    assert issue.level.value == "предупреждение"
    clean = second_search(
        module(identification="ПоПолямПоиска", search="Код, ЭтоГруппа"), "Код, ЭтоГруппа", "Код"
    )
    result = pair(module(), clean)
    assert found(result, "ed.pair.search_field_unsent") == []
    assert skipped(result, "ed.pair.search_field_unsent") == []
