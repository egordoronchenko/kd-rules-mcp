"""Общие модули и экспорт глобальных модулей берутся из XML-источников структуры."""

from pathlib import Path

from kd2_rules_mcp.service.ed_names import common_module_names
from kd2_rules_mcp.validation.ed_names import unknown_names


def test_xml_inventory_includes_extension_and_global_exports(tmp_path):
    base, extension = tmp_path / "base", tmp_path / "extension"
    for root, name, is_global in (
        (base, "ОбычныйМодуль", False),
        (extension, "ГлобальныйМодуль", True),
    ):
        folder = root / "CommonModules"
        source = folder / name / "Ext/Module.bsl"
        source.parent.mkdir(parents=True)
        (folder / f"{name}.xml").write_text(
            f"<CommonModule><Properties><Global>{str(is_global).lower()}</Global>"
            "</Properties></CommonModule>",
            encoding="utf-8",
        )
        source.write_text(
            "Функция ДоступныйМетод() Экспорт\nКонецФункции\n"
            'Функция СтроковыйАргумент(Вход = ")") // комментарий в сигнатуре\n'
            "Экспорт\nКонецФункции\n"
            "Процедура ЗакрытыйМетод()\nКонецПроцедуры\n",
            encoding="utf-8",
        )
    context = common_module_names(
        {"source": "xml", "source_path": str(base)}, Path, extensions=(str(extension),)
    )
    assert context is not None
    assert context == (
        ("ГлобальныйМодуль", "ОбычныйМодуль"),
        ("ДоступныйМетод", "СтроковыйАргумент"),
    )
    assert not unknown_names(
        "ОбычныйМодуль.Метод(); ДоступныйМетод(); СтроковыйАргумент();",
        (),
        common_modules=context[0],
        global_methods=context[1],
    )
    assert (
        unknown_names("ЗакрытыйМетод();", (), common_modules=context[0], global_methods=context[1])[
            0
        ].name
        == "ЗакрытыйМетод"
    )


def test_missing_module_inventory_is_not_an_empty_scope(tmp_path):
    assert common_module_names({"source": "md83exp"}, Path) is None
    assert (
        common_module_names({"source": "xml", "source_path": str(tmp_path / "missing")}, Path)
        is None
    )
