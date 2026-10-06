"""Обязательное свойство XDTO и пустой реквизит: источник, направление, ключ ссылки."""

import sqlite3
from dataclasses import replace
from pathlib import Path
from shutil import copytree
from types import MappingProxyType

import pytest

from kd2_rules_mcp.authoring.ed.context import AuthoringContext
from kd2_rules_mcp.authoring.ed.instruction import render_data_preflight
from kd2_rules_mcp.ed import compose_manager
from kd2_rules_mcp.ed.address import build_addresses
from kd2_rules_mcp.ed.schema import load_schema
from kd2_rules_mcp.ed.schema.profile import ValidationProfile
from kd2_rules_mcp.service import Kd2Service, Settings
from kd2_rules_mcp.service.ed_views import validation_view
from kd2_rules_mcp.validation.ed_authoring import check_profile
from kd2_rules_mcp.validation.ed_layers import select_context, validate_effective_schema
from kd2_rules_mcp.validation.ed_required import CHECK, RequiredUnfilled
from kd2_rules_mcp.validation.ed_schema import validate_schema
from kd2_rules_mcp.validation.report import Level, ValidationReport
from tests.test_ed_authoring_model import inputs as authoring_inputs
from tests.test_ed_profile import BASE, DATA, document
from tests.test_validation_ed_structure import snapshot


def required_report(
    tmp_path,
    *,
    fill="DontCheck",
    lower=1,
    nillable=False,
    key=True,
    direction="send",
    algorithm=False,
    conversion=False,
    source_type="Строка",
    structure=True,
):
    package = (DATA / "validation.bin").read_text(encoding="utf-8")
    package = package.replace(
        'xmlns:t="urn:test:validation"',
        'xmlns:t="urn:test:validation" xmlns:m="urn:test:writer-message"',
    ).replace(
        '<objectType name="Keys">',
        '<import namespace="urn:test:writer-message"/>'
        '<valueType name="TestRef" base="m:Ref"/>'
        '<objectType name="Keys"><property name="Ссылка" type="t:TestRef" nillable="true"/>',
    )
    leaf = (
        f'<property name="Код" type="xs:string" lowerBound="{lower}" '
        f'nillable="{str(nillable).lower()}"/>'
    )
    package = package.replace('<property name="Код" type="xs:string"/>', leaf)
    if not key:
        package = package.replace('<property name="КлючевыеСвойства" type="t:Keys"/>', leaf)
    path = tmp_path / "required.bin"
    path.write_text(package, encoding="utf-8")
    schema = load_schema(path, locate_import=lambda _: DATA.parent / "writer/message.bin")
    text = BASE.replace(
        'ДобавитьПКС(СвойстваШапки, "Код", "Код", 0);',
        f'ДобавитьПКС(СвойстваШапки, "Код", "Код", {int(algorithm)}'
        + (', "Выбор"' if conversion else "")
        + ");",
    )
    doc = document(text)
    snap = snapshot()
    obj = snap.objects[("справочник", "тест")]
    source = obj.property("Код")[0]
    obj = replace(
        obj,
        properties=MappingProxyType(
            {
                **obj.properties,
                ("код", ""): (replace(source, fill_checking=fill, types=(source_type,)),),
            }
        ),
    )
    snap = replace(snap, objects=MappingProxyType({**snap.objects, ("справочник", "тест"): obj}))
    risks = []
    report = validate_schema(
        doc,
        schema,
        build_addresses(doc),
        ValidationProfile.build(schema, "1.2", direction),
        snap if structure else None,
        required_unfilled=risks,
    )
    return report, risks


def test_required_source_without_fill_checking_names_documents(tmp_path):
    report, risks = required_report(tmp_path)
    issue = next(i for i in report.issues if i.check == CHECK)
    assert issue.address == "ПКО/Тест/ПКС/Код"
    assert issue.level.value == "предупреждение"
    assert "включая документы, а не только справочник" in issue.message
    assert "FillChecking=DontCheck" in issue.message
    assert len(risks) == 1 and risks[0].reference_key


@pytest.mark.parametrize(
    "options",
    [
        {"fill": "ShowError"},
        {"lower": 0},
        {"nillable": True},
        {"direction": "receive"},
        {"algorithm": True},
        {"source_type": "Булево"},
    ],
)
def test_required_unfilled_clean_boundaries(tmp_path, options):
    report, risks = required_report(tmp_path, **options)
    assert not [i for i in report.issues if i.check == CHECK]
    assert risks == []


@pytest.mark.parametrize("source_type", ["Строка", "Число", "Дата", "СправочникСсылка.Тест"])
def test_empty_values_omitted_before_xdto_assignment(tmp_path, source_type):
    report, risks = required_report(tmp_path, source_type=source_type, key=False)
    assert len([i for i in report.issues if i.check == CHECK]) == 1
    assert not risks[0].reference_key


def test_pkpd_conversion_does_not_make_empty_source_safe(tmp_path):
    report, risks = required_report(
        tmp_path, conversion=True, source_type="ПеречислениеСсылка.Выбор"
    )
    assert len([i for i in report.issues if i.check == CHECK]) == 1
    assert risks[0].reference_key


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"structure": False}, "structure_unavailable (supply structure_id)"),
        ({"fill": ""}, "fill_checking_unavailable (reload XML structure)"),
    ],
)
def test_missing_structure_or_old_md83exp_is_explicitly_skipped(tmp_path, options, reason):
    report, risks = required_report(tmp_path, **options)
    assert not risks
    assert any(s.check == CHECK and s.reason.startswith(reason) for s in report.skipped)


@pytest.mark.parametrize(
    "types,expression,parameters",
    [
        (("СправочникСсылка.Виды",), "ЗНАЧЕНИЕ(Справочник.Виды.ПустаяСсылка)", ""),
        (("ПеречислениеСсылка.Виды",), "&Пусто", "`Перечисления.Виды.ПустаяСсылка()`"),
        (("Число",), "&Пусто", "`&Пусто` = `0`"),
        (("Дата",), "&Пусто", "`&Пусто` = `Дата(1, 1, 1)`"),
        (
            ("Строка", "Число"),
            "&Пусто1 ИЛИ Объект.Источник = &Пусто2",
            "`&Неопределено` = `Неопределено`",
        ),
    ],
)
def test_preflight_queries_use_typed_empty_values(types, expression, parameters):
    risk = RequiredUnfilled(
        "ПКО/Пример/ПКС/Ключ", "Справочник", "Пример", "Источник", types, "Ключ", True
    )
    text = render_data_preflight([risk, replace(risk, address="ПКО/Другой/ПКС/Ключ")])
    assert text.count("ВЫБРАТЬ КОЛИЧЕСТВО(*)") == 1
    assert "ИЗ Справочник.Пример КАК Объект" in text
    assert "НЕ Объект.ПометкаУдаления И (Объект.Источник = " + expression in text
    assert parameters in text
    assert risk.address in text and "ПКО/Другой/ПКС/Ключ" in text
    assert "выгрузку документов" in text


def test_table_preflight_excludes_deleted_owner_and_empty_report_has_no_block():
    risk = RequiredUnfilled(
        "ПКО/Пример/ПКТЧ/Строки/ПКС/Сумма",
        "Документ",
        "Пример",
        "Строки.Сумма",
        ("Число",),
        "Сумма",
        False,
    )
    text = render_data_preflight([risk])
    assert "ИЗ Документ.Пример.Строки КАК Объект" in text
    assert "НЕ Объект.Ссылка.ПометкаУдаления И (Объект.Сумма = &Пусто)" in text
    assert render_data_preflight([]) == ""


def test_unfiltered_required_warnings_are_counted_and_details_are_paged():
    report = ValidationReport()
    report.error("ed.handler.missing", "ПКО/Другой", "Нет метода")
    for number in range(96):
        report.warning(CHECK, f"ПКО/Пример{number}/ПКС/Ключ", "Полный текст " * 100)
    original = tuple(report.issues)
    view = validation_view(report, None, None, None, "issues", 0, 5)
    assert view["summary"]["by_check"][CHECK] == 96
    assert view["summary"]["warnings"] == 96
    assert view["issues"]["total"] == 2
    grouped = view["issues"]["items"][1]
    assert grouped["check"] == CHECK and grouped["count"] == 96
    assert 'check_prefix="ed.schema.required_unfilled"' in grouped["message"]
    first = validation_view(report, None, CHECK, None, "issues", 0, 5)
    next_page = validation_view(report, None, CHECK, None, "issues", 5, 5)
    assert first["summary"] == view["summary"]
    assert first["issues"]["total"] == 96 and first["issues"]["has_more"]
    assert first["issues"]["items"] == [i.to_dict() for i in original[1:6]]
    assert next_page["issues"]["items"] == [i.to_dict() for i in original[6:11]]
    selected = validation_view(report, Level.WARNING.value, None, "ПКО/Пример42", "issues", 0, 5)
    assert selected["issues"]["items"] == [original[43].to_dict()]
    assert tuple(report.issues) == original


def test_force_reload_fill_checking_is_shared_by_direct_cached_and_layered_paths(tmp_path):
    from lxml import etree

    root = tmp_path / "dump"
    copytree(Path(__file__).parent / "data/xmldump/main", root)
    path = root / "Catalogs/Номенклатура.xml"
    tree = etree.parse(str(path))
    md = "http://v8.1c.ru/8.3/MDClasses"
    xr = "http://v8.1c.ru/8.3/xcf/readable"
    props = tree.find(f".//{{{md}}}Catalog/{{{md}}}Properties")
    assert props is not None
    standards = etree.SubElement(props, f"{{{md}}}StandardAttributes")
    code = etree.SubElement(standards, f"{{{xr}}}StandardAttribute", name="Code")
    etree.SubElement(code, f"{{{xr}}}FillChecking").text = "DontCheck"
    tree.write(str(path), encoding="utf-8")
    catalog = tmp_path / "projects.yaml"
    catalog.write_text(
        "projects:\n  demo:\n    name: Пример\n    configurations:\n      Main:\n        dump: .\n",
        encoding="utf-8",
    )
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            projects_file=catalog,
            project_dirs={"demo": root},
        )
    )
    service.structure_load_project("demo", "Main", structure_id="shared")
    connection = sqlite3.connect(service.store.path("shared"))
    try:
        with connection:
            connection.execute("ALTER TABLE properties DROP COLUMN fill_checking")
            connection.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
            connection.execute("UPDATE meta SET value='previous-reader' WHERE key='loader_version'")
    finally:
        connection.close()
    old, _ = service._ed_structure_snapshot("shared")
    doc = document(
        BASE.replace("Метаданные.Справочники.Тест", "Метаданные.Справочники.Номенклатура")
    )
    schema = load_schema(DATA / "validation.bin")
    profile = ValidationProfile.build(schema, "1.2", "send")
    layered = compose_manager(doc)
    context = select_context(layered, "send")
    cached_context = AuthoringContext(
        replace(
            authoring_inputs(),
            document=doc,
            schemas={"1.2": schema},
            structure=old,
        )
    )

    def reports(snap):
        values = (
            validate_schema(doc, schema, build_addresses(doc), profile, snap),
            check_profile(doc, schema, snap, "1.2", "send", context=cached_context),
            validate_effective_schema(layered, context, schema, profile, snap),
        )
        result = [
            (
                tuple(i for i in v.issues if i.check == CHECK),
                tuple(s for s in v.skipped if s.check == CHECK),
            )
            for v in values
        ]
        assert result[0] == result[1] == result[2]
        return result[0]

    issues, skips = reports(old)
    assert not issues and len(skips) == 1
    assert "fill_checking_unavailable (reload XML structure)" in skips[0].reason
    refreshed = service.structure_load_project("demo", "Main", structure_id="shared", force=True)
    assert not refreshed["reused"]
    fresh, _ = service._ed_structure_snapshot("shared")
    assert fresh is not old
    issues, skips = reports(fresh)
    assert not skips and len(issues) == 1
    assert "FillChecking=DontCheck" in issues[0].message
