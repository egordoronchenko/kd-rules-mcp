"""Пакет из файла и единый комплект; сравнение всех байтов и карта URI."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from lxml import etree

from kd_rules_mcp.authoring.ed.format_package import (
    FORMAT_OVERRIDE_MODULE,
    format_package_data,
    load_format_package,
    read_format_host,
    render_format_package,
)
from kd_rules_mcp.authoring.ed.manager_operations import ManagerOperation, PropertyPatch
from kd_rules_mcp.authoring.ed.manager_render import (
    ManagerManifest,
    ManagerRoute,
    render_manager_kit,
    render_manager_route,
)
from kd_rules_mcp.authoring.ed.manifest import sha256
from kd_rules_mcp.authoring.ed.model import AuthoringPreconditionError, ExtensionIdentity
from kd_rules_mcp.authoring.ed.xml_dump import ManagerHost, read_description
from kd_rules_mcp.ed.writer import new_manager, render
from tests.data.ed.format_package.model import OWN, sample_model
from tests.test_ed_format_package import declaration_model, write_files
from tests.test_ed_writer import execute
from tests.test_ed_writer_delivery import PLAN, build_kit
from tests.test_ed_writer_tables import table_model


def combined_kit(declaration, *, package=None, model=None, **kwargs):
    format_host = read_format_host(declaration.descriptions)
    plan = read_description(
        "ExchangePlans/TargetPlan.xml",
        declaration.descriptions["ExchangePlans/TargetPlan.xml"],
        "ExchangePlan",
    )
    host = ManagerHost(
        format_host.configuration_uuid,
        format_host.language,
        plan,
        format_host.compatibility_mode,
        ExtensionIdentity("fmt_Extension", "fmt_", ""),
        format_host.interface_compatibility_mode,
    )
    model = model or new_manager(manager_name="Manager")
    return render_manager_kit(
        model,
        render(model),
        host,
        ManagerRoute("TargetPlan", "1.20"),
        executor_profile_id="profile",
        format_package=package or sample_model(),
        format_declaration=declaration,
        **kwargs,
    )


def test_without_format_package_baseline_after_install_instruction_fix():
    # Способ Б меняет инструкцию, её хеш и печать версии; прочие файлы прежние.
    assert {p: sha256(b) for p, b in build_kit().files.items()} == {
        "extension/CommonModules/кд3м_PilotManager.xml": (
            "055835b7f0ed63ecead337cf4139d8c31839314f9ba8ed9c717ec8fe105aba80"
        ),
        "extension/CommonModules/кд3м_PilotManager/Ext/Module.bsl": (
            "b750ee0643ccb57a89ae4c3e5854e1dd6a1ce78af91220888c170d5edf3ea45c"
        ),
        "extension/Configuration.xml": (
            "f2dd67ed7d3121bbbe8ea4bf37901437c94764b2703eae43729b61e9cf2f2fa0"
        ),
        "extension/ExchangePlans/"
        + PLAN
        + ".xml": "8c35d3d7130c07ef73b169ab8703102e3b54b1186dccb329ea1225f9ef20a603",
        "extension/ExchangePlans/" + PLAN + "/Ext/ManagerModule.bsl": (
            "5641d5c3edb4ed1034a648469855da7f3a8b241e6230a69798255a84002c9809"
        ),
        "extension/Languages/Русский.xml": (
            "16594f5fedd17f8010f7f1ad55990eaf9d4a4f2cdacd4d10f3b00f7ba87d163d"
        ),
        "instruction.md": "35a85deb0f36281852119745f42c9eb18b1189c53e5d396fb28e2d06240680bf",
        "manager.ed.json": "9b6e7cc4924401cf51e5565e6eb0acf63919b82d23c109f3333aa5716a421b4a",
        "manifest.json": "d023c5d8f06f292c9cb088bd51ba5d9755f8ffafe9ed95c3de9071d1ab8f56aa",
        "source-map.json": "9106acf4b1a88b5bbfe9aa30fc774a839a465b79003b5fc12c2962f6ca7eb88e",
    }


def test_package_json_round_trip(tmp_path: Path):
    model = sample_model()
    path = tmp_path / "format-package.json"
    path.write_text(json.dumps(format_package_data(model)), encoding="utf-8")
    back = load_format_package(path, model.base_schema, "1.20")
    assert back == model
    assert render_format_package(back) == render_format_package(model)


def test_package_dump_folder_round_trip(tmp_path: Path):
    model = sample_model()
    root = write_files(tmp_path / "dump", render_format_package(model))
    back = load_format_package(root, model.base_schema, "1.20")
    before, after = render_format_package(model), render_format_package(back)
    assert after == before


def test_combined_extension_order_hooks_manifest_repeat(tmp_path: Path):
    declaration = declaration_model(tmp_path)
    kit = combined_kit(declaration)
    assert kit.files == combined_kit(declaration).files
    assert (
        ManagerManifest.from_bytes(kit.files["manifest.json"]).to_bytes()
        == kit.files["manifest.json"]
    )
    repeat = combined_kit(declaration, previous_manifest=kit.manifest, previous_files=kit.files)
    assert repeat.status == "unchanged" and repeat.files == kit.files
    children = etree.fromstring(kit.files["extension/Configuration.xml"]).find(
        "{*}Configuration/{*}ChildObjects"
    )
    assert children is not None
    assert [etree.QName(c).localname for c in children] == [
        "Language",
        "CommonModule",
        "CommonModule",
        "ExchangePlan",
        "XDTOPackage",
    ]
    assert [c.text for c in children if etree.QName(c).localname == "CommonModule"] == [
        "fmt_Manager",
        FORMAT_OVERRIDE_MODULE,
    ]
    package_path = "extension/XDTOPackages/fmt_Package/Ext/Package.bin"
    assert (
        kit.files[package_path]
        == render_format_package(sample_model())[package_path.removeprefix("extension/")]
    )
    route = kit.files["extension/ExchangePlans/TargetPlan/Ext/ManagerModule.bsl"].decode()
    assert route.count('&После("ПриПолученииНастроек")') == 1
    assert 'Настройки.ВерсииФорматаОбмена.Вставить("1.20", fmt_Manager)' in route
    assert f'Настройки.РасширенияФорматаОбмена.Вставить("{OWN}", "1.20")' in route
    override = kit.files[
        f"extension/CommonModules/{FORMAT_OVERRIDE_MODULE}/Ext/Module.bsl"
    ].decode()
    assert f'РасширенияФормата.Вставить("{OWN}", "1.20")' in override
    manifest = json.loads(kit.files["manifest.json"])
    assert manifest["format_package"]["sha256"] == sha256(kit.files[package_path])
    assert manifest["format_package"]["imports"] == {
        sample_model().base_namespace: sample_model().base_schema.packages[0].sources[0].sha256
    }
    assert manifest["format_extensions"] == {OWN: "1.20"}
    assert "на обе стороны" in kit.instruction
    assert "снимите безопасный режим" in kit.instruction
    assert "версия узла = версия привязки" in kit.instruction
    assert "Если у корреспондента расширения нет" in kit.instruction
    borrowed = kit.manifest.identity_map.borrowed
    assert "commonmodule/" + FORMAT_OVERRIDE_MODULE.casefold() in borrowed


def test_package_instruction_installation_checks_order_and_manual_parts(tmp_path: Path):
    instruction = combined_kit(declaration_model(tmp_path)).instruction
    assert instruction.index("## Шаг 4.") < instruction.index("## Шаг 5.1.")
    assert instruction.index("## Шаг 5.1.") < instruction.index("## Шаг 6.")
    assert instruction.count(f'ФабрикаXDTO.Пакеты.Получить("{OWN}")') == 2
    assert instruction.count('ДоступныеРасширенияФормата("1.20")') == 2
    assert "для vcexecutecode" in instruction
    assert "BX:" not in instruction
    assert 'Получить(Новый Структура("Имя", Имя))' in instruction
    assert (
        instruction.index("Расширение = Найденные[0];")
        < instruction.index("Расширение.ПроверитьВозможностьПрименения(Данные, Истина)")
        < instruction.index("Расширение.Записать(Данные)")
    )
    assert "ВыполнитьЗагрузкуДляУзлаИнформационнойБазыЧерезСтроку" in instruction
    assert "ОбновитьНастройкиXDTOКорреспондента" in instruction
    assert "не подтверждает наличие пакета" in instruction
    assert "URI в заголовке сообщения не публикует" in instruction
    assert "молча пропускает свойство" in instruction
    assert "стирает реквизит прямой ПКС" in instruction
    assert "не привязывайте ПКС на получение" in instruction
    manual = instruction.split("## Если переносите руками", 1)[1].split("## Что проверено", 1)[0]
    assert "**четыре части**" in manual
    for part in (
        "1. **Общий модуль",
        "2. **Пакет XDTO**",
        "3. **Заимствованный общий модуль",
        "4. **Дополнение ПриПолученииНастроек**",
        "ВерсииФорматаОбмена",
        "РасширенияФорматаОбмена",
    ):
        assert part in manual


@pytest.mark.parametrize("settings_parameter", ["Настройки", "НастройкиПлана"])
def test_package_route_preserves_baseline_bytes_outside_added_parts(settings_parameter):
    route = ManagerRoute("TargetPlan", "1.20")
    baseline = render_manager_route(route, module_name="fmt_Manager", prefix="fmt_")
    combined = render_manager_route(
        route,
        module_name="fmt_Manager",
        prefix="fmt_",
        format_namespace=OWN,
        settings_parameter=settings_parameter,
    )
    added = (
        '\tЕсли ТипЗнч(Настройки.РасширенияФорматаОбмена) = Тип("Соответствие") Тогда\n'
        f'\t\tНастройки.РасширенияФорматаОбмена.Вставить("{OWN}", "1.20");\n'
        "\tКонецЕсли;\n"
    )
    assert added in combined
    assert combined.replace(added, "").encode("utf-8") == baseline.encode("utf-8")


def test_stub_instruction_order_after_repeat_and_reverting_decisions():
    from kd_rules_mcp.service.ed_preflight import with_key_data_instruction

    stubs = ({"object": "Справочник.Item", "name": "StubItem"},)
    data = "\n## Проверьте данные перед первым обменом\n\n### Справочник.Item.Name\n\nЗапрос.\n"
    keys = "\n## Проверьте данные перед первым обменом\n\n### Справочник.Item.Code\n\nКлюч.\n"

    def kit(*, previous=None, extra_data=""):
        result = build_kit(
            pod_stubs=stubs,
            data_preflight=data + extra_data,
            previous_manifest=previous.manifest if previous else None,
            previous_files=previous.files if previous else None,
        )
        return with_key_data_instruction(
            result,
            keys,
            previous.manifest if previous else None,
            dict(previous.files) if previous else {},
        )

    first = kit()
    repeated = kit(previous=first)
    assert repeated.status == "unchanged" and repeated.files == first.files
    changed = kit(previous=first, extra_data="\n### Справочник.Item.Other\n\nДругой запрос.\n")
    reverted = kit(previous=changed)
    assert reverted.instruction == first.instruction
    assert reverted.instruction.count("## Заглушки ПОД") == 1
    assert reverted.instruction.index("## Заглушки ПОД") < reverted.instruction.index(
        "## Проверьте данные перед первым обменом"
    )


def test_package_and_import_hashes_change_decision(tmp_path: Path):
    declaration = declaration_model(tmp_path)
    package = sample_model()
    first = combined_kit(declaration, package=package)
    changed = replace(package, types=(*package.types[:-1], replace(package.types[-1], open=False)))
    assert (
        combined_kit(declaration, package=changed).manifest.decision_hash
        != first.manifest.decision_hash
    )
    source = replace(package.base_schema.packages[0].sources[0], sha256="0" * 64)
    base = replace(
        package.base_schema, packages=(replace(package.base_schema.packages[0], sources=(source,)),)
    )
    assert (
        combined_kit(declaration, package=replace(package, base_schema=base)).manifest.decision_hash
        != first.manifest.decision_hash
    )


def test_unknown_property_uri_refuses(tmp_path: Path):
    model = table_model()
    model = execute(
        model,
        ManagerOperation(
            "column",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            patch=PropertyPatch(
                configuration_property="A", format_property="A", namespace="urn:unknown"
            ),
        ),
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        combined_kit(declaration_model(tmp_path), model=model)
    assert caught.value.failures[0].id == "ed.author.format_uri_unknown"


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        "{}",
        '{"namespace": 1}',
        '{"unknown": "unused"}',
        '{"namespace": "first", "namespace": "second"}',
    ],
)
def test_invalid_json_refuses(tmp_path: Path, content):
    path = tmp_path / "format-package.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AuthoringPreconditionError) as caught:
        load_format_package(path, sample_model().base_schema, "1.20")
    assert caught.value.failures[0].id == "ed.author.format_package_invalid"


def test_invalid_empty_dump_refuses(tmp_path: Path):
    with pytest.raises(AuthoringPreconditionError) as caught:
        load_format_package(tmp_path, sample_model().base_schema, "1.20")
    assert caught.value.failures[0].id == "ed.author.format_package_invalid"


def test_broken_dump_bin_refuses(tmp_path: Path):
    package = sample_model()
    root = write_files(tmp_path, render_format_package(package))
    (root / "XDTOPackages/fmt_Package/Ext/Package.bin").write_bytes(b"<broken>")
    with pytest.raises(AuthoringPreconditionError) as caught:
        load_format_package(root, package.base_schema, "1.20")
    assert caught.value.failures[0].id == "ed.author.format_package_invalid"
