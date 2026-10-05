"""Доставка W1 на собственных моделях: XML, владение, дельта и адреса текста."""

import json
import re
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from lxml import etree

from kd2_rules_mcp.authoring.ed.manager_operations import ManagerOperation, PropertyPatch
from kd2_rules_mcp.authoring.ed.manager_render import (
    REQUIRED_ROUTINES,
    ManagerManifest,
    ManagerRoute,
    render_manager_kit,
)
from kd2_rules_mcp.authoring.ed.manifest import sha256
from kd2_rules_mcp.authoring.ed.model import AuthoringPreconditionError, ExtensionIdentity
from kd2_rules_mcp.authoring.ed.xml_dump import (
    Description,
    ManagerHost,
    profile_template,
    read_manager_host,
)
from kd2_rules_mcp.ed.canonical import canonicalize
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import import_manager
from kd2_rules_mcp.ed.writer_model import FormatBinding, load_model
from tests.test_ed_writer import execute, pilot_model

PLAN = "СинхронизацияДанныхЧерезУниверсальныйФормат"
PROFILE = "opaque-profile-id"
DATA = Path(__file__).parent / "data/ed/writer/pilot.bsl"


def host_metadata(prefix="кд3м_"):
    return ManagerHost(
        "11111111-1111-4111-8111-111111111111",
        Description(
            "Languages/Русский.xml",
            "Language",
            "Русский",
            "22222222-2222-4222-8222-222222222222",
            {"LanguageCode": "ru"},
        ),
        Description(
            "ExchangePlans/" + PLAN + ".xml",
            "ExchangePlan",
            PLAN,
            "33333333-3333-4333-8333-333333333333",
            {},
        ),
        compatibility_mode="Version8_3_27",
        identity=ExtensionIdentity("кд3м_Менеджер", prefix, "Менеджер обмена ED"),
        interface_compatibility_mode="TaxiEnableVersion8_2",
    )


def golden_model():
    return import_manager(
        read_manager_text(DATA.read_bytes().decode("utf-8")),
        project_id="delivery",
        manager_name="PilotManager",
    )[0]


def build_kit(model=None, *, host=None, route=None, **kwargs):
    model = model or golden_model()
    return render_manager_kit(
        model,
        render(model),
        host or host_metadata(),
        route or ManagerRoute(PLAN, "1.20"),
        executor_profile_id=kwargs.pop("executor_profile_id", PROFILE),
        **kwargs,
    )


def xml_object(kit, path):
    return etree.fromstring(kit.files["extension/" + path])[0]


def properties(obj):
    return {etree.QName(p).localname: p.text or "" for p in obj.find("{*}Properties")}


def changed_files(before, after):
    return {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}


@pytest.mark.parametrize("factory", [golden_model, new_manager])
def test_kit_files_xml_and_snapshot(factory):
    model = factory()
    kit = build_kit(model)
    name = kit.module_name
    assert set(kit.files) == {
        "extension/Configuration.xml",
        "extension/Languages/Русский.xml",
        "extension/CommonModules/" + name + ".xml",
        "extension/CommonModules/" + name + "/Ext/Module.bsl",
        "extension/ExchangePlans/" + PLAN + ".xml",
        "extension/ExchangePlans/" + PLAN + "/Ext/ManagerModule.bsl",
        "manager.ed.json",
        "source-map.json",
        "manifest.json",
        "instruction.md",
    }
    config = xml_object(kit, "Configuration.xml")
    props = properties(config)
    assert {
        k: props[k]
        for k in (
            "ObjectBelonging",
            "ConfigurationExtensionPurpose",
            "NamePrefix",
            "KeepMappingToExtendedConfigurationObjectsByIDs",
            "ConfigurationExtensionCompatibilityMode",
            "DefaultLanguage",
            "Version",
        )
    } == {
        "ObjectBelonging": "Adopted",
        "ConfigurationExtensionPurpose": "Customization",
        "NamePrefix": "кд3м_",
        "KeepMappingToExtendedConfigurationObjectsByIDs": "true",
        "ConfigurationExtensionCompatibilityMode": "Version8_3_27",
        "DefaultLanguage": "Language.Русский",
        "Version": kit.manifest.decision_hash[:12],
    }
    contained = config.findall("{*}InternalInfo/{*}ContainedObject")
    assert [n.findtext("{*}ClassId") for n in contained] == profile_template()["class_ids"]
    assert len(contained) == 7
    children = config.find("{*}ChildObjects")
    assert children is not None
    assert [(etree.QName(n).localname, n.text) for n in children] == [
        ("Language", "Русский"),
        ("CommonModule", name),
        ("ExchangePlan", PLAN),
    ]
    language = properties(xml_object(kit, "Languages/Русский.xml"))
    assert language["ObjectBelonging"] == "Adopted"
    assert language["ExtendedConfigurationObject"] == host_metadata().language.uuid
    module = xml_object(kit, "CommonModules/" + name + ".xml")
    module_props = properties(module)
    assert (
        "ObjectBelonging" not in module_props and "ExtendedConfigurationObject" not in module_props
    )
    synonym = module.find("{*}Properties/{*}Synonym/{*}item/{*}content")
    assert synonym is not None and synonym.text
    assert {
        k: module_props[k]
        for k in (
            "Global",
            "ClientManagedApplication",
            "Server",
            "ExternalConnection",
            "ClientOrdinaryApplication",
            "ServerCall",
            "Privileged",
            "ReturnValuesReuse",
        )
    } == {
        "Global": "false",
        "ClientManagedApplication": "false",
        "Server": "true",
        "ExternalConnection": "true",
        "ClientOrdinaryApplication": "true",
        "ServerCall": "false",
        "Privileged": "false",
        "ReturnValuesReuse": "DontUse",
    }
    plan = xml_object(kit, "ExchangePlans/" + PLAN + ".xml")
    assert properties(plan)["ExtendedConfigurationObject"] == host_metadata().exchange_plan.uuid
    assert properties(plan)["ObjectBelonging"] == "Adopted"
    assert UUID(plan.findtext("{*}InternalInfo/{*}ThisNode")).version == 5
    generated = plan.findall("{*}InternalInfo/{*}GeneratedType")
    assert [n.get("category") for n in generated] == [
        "Object",
        "Ref",
        "Selection",
        "List",
        "Manager",
    ]
    assert all(
        n.get("name") == "ExchangePlan" + str(n.get("category")) + "." + PLAN for n in generated
    )
    assert plan.findtext("{*}InternalInfo/{*}PropertyState/{*}Property") == "ManagerModule"
    assert plan.findtext("{*}InternalInfo/{*}PropertyState/{*}State") == "Extended"
    children = plan.find("{*}ChildObjects")
    assert children is not None and len(children) == 0
    assert all(UUID(v).version == 5 for v in kit.manifest.identity_map.objects.values())
    assert len(set(kit.manifest.identity_map.objects.values())) == 22
    assert canonicalize(load_model(kit.files["manager.ed.json"])) == canonicalize(model)
    payload = json.loads(kit.files["manifest.json"])
    assert payload["generator_version"] == "ed-manager/1"
    assert payload["runtime_verified"] is False
    assert payload["inputs"]["executor_profile_id"] == PROFILE
    assert payload["input_hashes"]["manager.ed.json"] == sha256(kit.files["manager.ed.json"])
    assert kit.manifest.file_hashes == {
        p: sha256(b) for p, b in kit.files.items() if p != "manifest.json"
    }
    assert (
        ManagerManifest.from_bytes(kit.files["manifest.json"]).to_bytes()
        == kit.files["manifest.json"]
    )


def test_deterministic_build_and_previous_repeat():
    model = golden_model()
    first, second = build_kit(model), build_kit(model)
    assert first.files == second.files
    repeated = build_kit(model, previous_manifest=first.manifest, previous_files=first.files)
    assert repeated.status == "unchanged" and repeated.files == first.files
    assert (
        build_kit(
            model, previous_manifest=first.manifest, previous_files=first.files, keep_version=True
        ).files
        == first.files
    )


def test_exact_key_changes_only_route_stamp_and_report():
    model = golden_model()
    first = build_kit(model)
    second = build_kit(
        model,
        route=ManagerRoute(PLAN, "1.20.0"),
        previous_manifest=first.manifest,
        previous_files=first.files,
    )
    assert changed_files(first.files, second.files) == {
        "extension/Configuration.xml",
        "extension/ExchangePlans/" + PLAN + "/Ext/ManagerModule.bsl",
        "instruction.md",
        "manifest.json",
    }
    hook = second.files["extension/ExchangePlans/" + PLAN + "/Ext/ManagerModule.bsl"].decode()
    assert 'Вставить("1.20.0",' in hook and 'Вставить("1.20",' not in hook
    assert (
        properties(xml_object(first, "Configuration.xml"))["Version"]
        != properties(xml_object(second, "Configuration.xml"))["Version"]
    )
    assert second.manifest.changes == {"added": (), "changed": (), "removed": ()}


def test_prefix_changes_names_and_own_module_uuid_only():
    model = golden_model()
    first = build_kit(model)
    second = build_kit(
        model,
        host=host_metadata("доп_"),
        previous_manifest=first.manifest,
        previous_files=first.files,
    )
    old, new = first.module_name, second.module_name
    assert new == "доп_PilotManager"
    assert changed_files(first.files, second.files) == {
        "extension/Configuration.xml",
        "extension/CommonModules/" + old + ".xml",
        "extension/CommonModules/" + new + ".xml",
        "extension/CommonModules/" + old + "/Ext/Module.bsl",
        "extension/CommonModules/" + new + "/Ext/Module.bsl",
        "extension/ExchangePlans/" + PLAN + "/Ext/ManagerModule.bsl",
        "source-map.json",
        "instruction.md",
        "manifest.json",
    }
    assert (
        first.files["extension/CommonModules/" + old + "/Ext/Module.bsl"]
        == second.files["extension/CommonModules/" + new + "/Ext/Module.bsl"]
    )
    assert xml_object(first, "CommonModules/" + old + ".xml").get("uuid") != xml_object(
        second, "CommonModules/" + new + ".xml"
    ).get("uuid")
    assert second.manifest.changes == {"added": (), "changed": (), "removed": ()}


def test_one_property_delta_and_unchanged_rebuild():
    model = pilot_model()
    first = build_kit(model)
    prop = model.pko[0].properties[0]
    changed = execute(
        model,
        ManagerOperation(
            "edit-property",
            "property",
            "update",
            target_id=prop.logical_id,
            patch=PropertyPatch(format_property="ПолноеНаименование"),
        ),
    )
    second = build_kit(changed, previous_manifest=first.manifest, previous_files=first.files)
    assert changed_files(first.files, second.files) == {
        "extension/Configuration.xml",
        "extension/CommonModules/" + first.module_name + "/Ext/Module.bsl",
        "manager.ed.json",
        "source-map.json",
        "manifest.json",
        "instruction.md",
    }
    assert second.manifest.changes == {"added": (), "changed": (prop.logical_id,), "removed": ()}
    assert (
        build_kit(changed, previous_manifest=second.manifest, previous_files=second.files).files
        == second.files
    )


def test_added_and_removed_entities():
    model = pilot_model()
    first = build_kit(model)
    added = execute(
        model,
        ManagerOperation(
            "new-property",
            "property",
            "create",
            owner_id=model.pko[0].logical_id,
            container_id=model.pko[0].logical_id,
            patch=PropertyPatch(configuration_property="Код", format_property="Код"),
        ),
    )
    second = build_kit(added, previous_manifest=first.manifest, previous_files=first.files)
    added_id = next(
        p.logical_id for p in added.pko[0].properties if p.configuration_property == "Код"
    )
    assert added_id in second.manifest.changes["added"]
    third = build_kit(model, previous_manifest=second.manifest, previous_files=second.files)
    assert added_id in third.manifest.changes["removed"]


def test_text_only_delta_is_visible():
    """Та же модель, другой текст модуля: меняется только оформление одной ПКС.

    Ширину выравнивания ПКО для этого брать нельзя: стиль модуля применяется к порождению и без
    подсказок листа. Хвостовая табуляция одной строки ПКС — подсказка только этого листа.
    """
    lines = DATA.read_bytes().decode("utf-8").split("\n")
    target = next(i for i, line in enumerate(lines) if line.lstrip().startswith("ДобавитьПКС("))
    carriage = "\r" if lines[target].endswith("\r") else ""
    lines[target] = lines[target].rstrip("\r") + "\t" + carriage
    model = import_manager(
        read_manager_text("\n".join(lines)), project_id="text-only", manager_name="PilotManager"
    )[0]
    first = render_manager_kit(
        model,
        render(model, "canonical"),
        host_metadata(),
        ManagerRoute(PLAN, "1.20"),
        executor_profile_id=PROFILE,
    )
    rendered = render(model, "canonical", use_source_style=False)
    second = render_manager_kit(
        model,
        rendered,
        host_metadata(),
        ManagerRoute(PLAN, "1.20"),
        executor_profile_id=PROFILE,
        previous_manifest=first.manifest,
        previous_files=first.files,
    )
    assert first.files["manager.ed.json"] == second.files["manager.ed.json"]
    assert first.manifest.inputs["module_sha256"] != second.manifest.inputs["module_sha256"]
    properties_ids = {p.logical_id for rule in model.pko for p in rule.properties}
    changed = second.manifest.changes["changed"]
    assert len(changed) == 1 and changed[0] in properties_ids


@pytest.mark.parametrize("interface", [1, 3])
def test_unsupported_interface(interface):
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(new_manager(interface_version=interface))
    assert error.value.failures[0].id == "ed.author.unsupported_form"
    assert error.value.failures[0].address


@pytest.mark.parametrize("key", [("1.20", "1.21"), (), "", "1.20\n"])
def test_route_scope_refusals(key):
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(route=ManagerRoute(PLAN, key))
    assert error.value.failures[0].id == "ed.author.route_scope_conflict"
    assert error.value.failures[0].address


def test_route_plan_conflict():
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(route=ManagerRoute("ДругойПлан", "1.20"))
    assert error.value.failures[0].id == "ed.author.route_scope_conflict"


@pytest.mark.parametrize(
    "prefix,name",
    [
        ("1_", "Manager"),
        ("плохой-префикс", "Manager"),
        ("", "Manager"),
        ("кд3м_", "Имя.Суффикс"),
        ("кд3м_", "Если"),
        ("_ ", "Manager"),
        ("кд3м_", "1Имя"),
    ],
)
def test_invalid_names(prefix, name):
    model = new_manager()
    model = replace(model, header=replace(model.header, manager_name=name)).with_revision()
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(model, host=host_metadata(prefix))
    assert error.value.failures[0].id == "ed.author.identifier_conflict"
    assert error.value.failures[0].address


@pytest.mark.parametrize("routine_name", REQUIRED_ROUTINES)
def test_missing_required_export_refused(routine_name):
    model = new_manager()
    rendered = render(model)
    document = read_manager_text(rendered.data.decode("utf-8"))
    routine = next(r for r in document.routines if r.name == routine_name)
    text = rendered.text.replace(routine.raw_text, "")
    broken = replace(rendered, text=text, data=("\ufeff" + text).encode("utf-8"))
    with pytest.raises(AuthoringPreconditionError) as error:
        render_manager_kit(
            model, broken, host_metadata(), ManagerRoute(PLAN, "1.20"), executor_profile_id=PROFILE
        )
    assert error.value.failures[0].id == "ed.author.model_invalid"
    assert error.value.failures[0].address == "Код/" + routine_name


@pytest.mark.parametrize("mutation", ["partial", "different", "bytes", "report", "not-exported"])
def test_invalid_generated_model(mutation):
    model = pilot_model()
    rendered = render(model)
    if mutation == "partial":
        text = rendered.text + "\nНеизвестныйВызов();\n"
        rendered = replace(rendered, text=text, data=("\ufeff" + text).encode())
    elif mutation == "different":
        rendered = render(new_manager())
    elif mutation == "bytes":
        rendered = replace(rendered, data=rendered.data + b" ")
    elif mutation == "report":
        rendered = replace(rendered, report=replace(rendered.report, entries=()))
    else:
        text = rendered.text.replace(
            "ПередКонвертацией(КомпонентыОбмена) Экспорт", "ПередКонвертацией(КомпонентыОбмена)"
        )
        rendered = replace(rendered, text=text, data=("\ufeff" + text).encode())
    with pytest.raises(AuthoringPreconditionError) as error:
        render_manager_kit(
            model,
            rendered,
            host_metadata(),
            ManagerRoute(PLAN, "1.20"),
            executor_profile_id=PROFILE,
        )
    assert error.value.failures[0].id == "ed.author.model_invalid"
    assert error.value.failures[0].address


@pytest.mark.parametrize("mutation", ["foreign", "changed", "missing", "manifest"])
def test_previous_files_ownership(mutation):
    model = golden_model()
    first = build_kit(model)
    files, manifest = dict(first.files), first.manifest
    if mutation == "foreign":
        files["foreign.txt"] = b"not ours"
    elif mutation == "missing":
        del files["source-map.json"]
    elif mutation == "manifest":
        files["manifest.json"] += b" "
    else:
        path = "extension/CommonModules/" + first.module_name + "/Ext/Module.bsl"
        files[path] += b"\n// manual\n"
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(model, previous_manifest=manifest, previous_files=files)
    assert error.value.failures[0].id == "ed.author.owned_content_changed"


@pytest.mark.parametrize("newline,bom", [("\n", False), ("\r\n", True)])
def test_source_map_matches_reread_procedures(newline, bom):
    text = DATA.read_bytes().decode("utf-8-sig").replace("\r\n", "\n").replace("\n", newline)
    model = import_manager(
        read_manager_text(("\ufeff" if bom else "") + text),
        project_id="source-map",
        manager_name="PilotManager",
    )[0]
    kit = build_kit(model)
    data = kit.files["extension/CommonModules/" + kit.module_name + "/Ext/Module.bsl"]
    document = read_manager_text(data.decode())
    source_map = json.loads(kit.files["source-map.json"])
    routines = {r.name: r for r in document.routines}
    lines = data.decode("utf-8-sig").splitlines()
    for rule in (*model.pko, *model.pod):
        row, routine = source_map[rule.logical_id], routines[rule.procedure_name]
        assert row["line_start"] == routine.span.line_start
        assert row["line_end"] == routine.span.line_end
        assert rule.procedure_name in lines[row["line_start"] - 1]
    for rule in model.pko:
        routine = routines[rule.procedure_name]
        for prop in rule.properties:
            row = source_map[prop.logical_id]
            assert (
                routine.span.line_start
                < row["line_start"]
                <= row["line_end"]
                < routine.span.line_end
            )
            assert prop.configuration_property in data[row["byte_start"] : row["byte_end"]].decode()


def test_instruction_counts_evidence_and_profile_hash():
    model = pilot_model()
    first = build_kit(model)
    evidence = {"Интерфейс 2: маршрут с узлом": True, "Поиск по наименованию": False}
    second = build_kit(model, form_evidence=evidence)
    assert "| Отправка | 1 | 1 | 2 | 3 |" in second.instruction
    assert "| Получение | 1 | 1 | 2 | 2 |" in second.instruction
    assert "| Интерфейс 2: маршрут с узлом | Да |" in second.instruction
    assert "| Поиск по наименованию | Нет |" in second.instruction
    assert not re.search(r"\$\{|\[if |\[/if\]", second.instruction)
    assert "**все**" in second.instruction and "**всех**" in second.instruction
    assert "обработчики" in second.instruction and "перестаёт действовать" in second.instruction
    assert second.manifest.form_evidence == evidence and second.manifest.runtime_verified is False
    assert first.manifest.decision_hash == second.manifest.decision_hash
    assert changed_files(first.files, second.files) == {"manifest.json", "instruction.md"}
    third = build_kit(model, executor_profile_id="other-profile")
    assert third.manifest.decision_hash != first.manifest.decision_hash
    empty = build_kit(new_manager())
    assert "| Отправка | 0 | 0 | 0 | 0 |" in empty.instruction
    assert "| Получение | 0 | 0 | 0 | 0 |" in empty.instruction


def test_host_and_delivery_template_are_decision_inputs(monkeypatch):
    from kd2_rules_mcp.authoring.ed import manager_render

    model = golden_model()
    first = build_kit(model)
    host = host_metadata()
    host = replace(
        host, language=replace(host.language, uuid="44444444-4444-4444-8444-444444444444")
    )
    second = build_kit(
        model, host=host, previous_manifest=first.manifest, previous_files=first.files
    )
    assert changed_files(first.files, second.files) == {
        "extension/Configuration.xml",
        "extension/Languages/Русский.xml",
        "manifest.json",
        "instruction.md",
    }
    assert second.manifest.decision_hash != first.manifest.decision_hash
    monkeypatch.setattr(manager_render, "MANAGER_TEMPLATE_VERSION", "ed-manager-delivery/test")
    third = build_kit(model)
    assert third.manifest.decision_hash != first.manifest.decision_hash
    assert changed_files(first.files, third.files) == {
        "extension/Configuration.xml",
        "manifest.json",
        "instruction.md",
    }


def test_single_key_tuple_and_prefixed_name():
    model = new_manager(manager_name="кд3м_МенеджерОбмена")
    first = build_kit(model)
    second = build_kit(model, route=ManagerRoute(PLAN, ("1.20",)))
    assert first.module_name == "кд3м_МенеджерОбмена"
    assert first.files == second.files


@pytest.mark.parametrize("part", ["manifest", "files"])
def test_previous_inputs_require_both_parts(part):
    first = build_kit()
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(
            previous_manifest=first.manifest if part == "manifest" else None,
            previous_files=first.files if part == "files" else None,
        )
    assert error.value.failures[0].id == "ed.author.owned_content_changed"


def test_changed_decisions_cannot_keep_old_stamp():
    model = golden_model()
    first = build_kit(model)
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(
            model,
            route=ManagerRoute(PLAN, "1.21"),
            previous_manifest=first.manifest,
            previous_files=first.files,
            keep_version=True,
        )
    assert error.value.failures[0].id == "ed.author.model_invalid"
    assert error.value.failures[0].address == "Расширение/Версия"


def test_read_host_from_original_descriptions():
    host = host_metadata()

    def description(kind, name, uuid, extra=""):
        return (
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" version="2.20">'
            f'<{kind} uuid="{uuid}"><Properties><Name>{name}</Name>{extra}</Properties>'
            f"</{kind}></MetaDataObject>"
        )

    descriptions = {
        "Configuration.xml": description(
            "Configuration",
            "Пример",
            host.configuration_uuid,
            "<DefaultLanguage>Language.Русский</DefaultLanguage><CompatibilityMode>DontUse</CompatibilityMode>"
            "<InterfaceCompatibilityMode>Taxi</InterfaceCompatibilityMode>",
        ),
        "Languages/Русский.xml": description(
            "Language", "Русский", host.language.uuid, "<LanguageCode>ru</LanguageCode>"
        ),
        "ExchangePlans/" + PLAN + ".xml": description(
            "ExchangePlan", PLAN, host.exchange_plan.uuid
        ),
    }
    read = read_manager_host(descriptions, PLAN)
    assert read.configuration_uuid == host.configuration_uuid
    assert (
        read.language.uuid == host.language.uuid
        and read.exchange_plan.uuid == host.exchange_plan.uuid
    )
    assert read.compatibility_mode == "DontUse"
    assert read.interface_compatibility_mode == "Taxi"
    assert read.identity.prefix == "кд3м_"


def host_descriptions(compatibility="Version8_3_27", interface="TaxiEnableVersion8_2"):
    """Минимальная выгрузка; режим расширений намеренно отличается от режима конфигурации."""
    host = host_metadata()

    def description(kind, name, uuid, extra=""):
        return (
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" version="2.20">'
            f'<{kind} uuid="{uuid}"><Properties><Name>{name}</Name>{extra}</Properties>'
            f"</{kind}></MetaDataObject>"
        )

    modes = (
        "<ConfigurationExtensionCompatibilityMode>Version8_3_27"
        "</ConfigurationExtensionCompatibilityMode>"
    )
    if compatibility is not None:
        modes += f"<CompatibilityMode>{compatibility}</CompatibilityMode>"
    if interface is not None:
        modes += f"<InterfaceCompatibilityMode>{interface}</InterfaceCompatibilityMode>"
    return {
        "Configuration.xml": description(
            "Configuration",
            "Пример",
            host.configuration_uuid,
            "<DefaultLanguage>Language.Русский</DefaultLanguage>" + modes,
        ),
        "Languages/Русский.xml": description(
            "Language", "Русский", host.language.uuid, "<LanguageCode>ru</LanguageCode>"
        ),
        "ExchangePlans/" + PLAN + ".xml": description(
            "ExchangePlan", PLAN, host.exchange_plan.uuid
        ),
    }


@pytest.mark.parametrize(
    "compatibility,interface",
    [
        ("Version8_3_24", "TaxiEnableVersion8_2"),
        ("Version8_3_27", "Taxi"),
        ("Version8_3_14", "Taxi"),
        ("DontUse", "Taxi"),
    ],
)
def test_review_host_modes_are_inherited_and_hashed(compatibility, interface):
    first = build_kit(host=read_manager_host(host_descriptions(), PLAN))
    host = read_manager_host(host_descriptions(compatibility, interface), PLAN)
    second = build_kit(host=host)
    props = properties(xml_object(second, "Configuration.xml"))
    assert props["ConfigurationExtensionCompatibilityMode"] == compatibility
    assert props["InterfaceCompatibilityMode"] == interface
    assert second.manifest.inputs["host"]["compatibility_mode"] == compatibility
    assert second.manifest.inputs["host"]["interface_compatibility_mode"] == interface
    assert second.manifest.input_hashes["host"] != first.manifest.input_hashes["host"]
    assert second.manifest.decision_hash != first.manifest.decision_hash


@pytest.mark.parametrize("missing", ["CompatibilityMode", "InterfaceCompatibilityMode"])
@pytest.mark.parametrize("value", [None, ""])
def test_review_missing_host_mode_refused(missing, value):
    kwargs = {"compatibility" if missing == "CompatibilityMode" else "interface": value}
    with pytest.raises(AuthoringPreconditionError) as error:
        read_manager_host(host_descriptions(**kwargs), PLAN)
    failure = error.value.failures[0]
    assert failure.id == "ed.author.metadata_profile_unsupported"
    assert missing in failure.message and failure.address


@pytest.mark.parametrize("legacy_output", ["version", "instruction", "xml", "writer"])
def test_review_previous_manifest_owns_old_generator_bytes(monkeypatch, legacy_output):
    from kd2_rules_mcp.authoring.ed import manager_render

    current_version = manager_render.MANAGER_TEMPLATE_VERSION
    if legacy_output == "version":
        monkeypatch.setattr(manager_render, "MANAGER_TEMPLATE_VERSION", "ed-manager-delivery/0-old")
    model = golden_model()
    old = build_kit(model)
    files = dict(old.files)
    path = {
        "instruction": "instruction.md",
        "xml": "extension/Configuration.xml",
        "writer": "extension/CommonModules/" + old.module_name + "/Ext/Module.bsl",
    }.get(legacy_output)
    if path:
        files[path] += (
            b"\n<!-- old delivery output -->\n"
            if legacy_output == "xml"
            else b"\n// old delivery output\n"
        )
    previous = replace(
        old.manifest, file_hashes={p: sha256(files[p]) for p in old.manifest.file_hashes}
    )
    files["manifest.json"] = previous.to_bytes()
    monkeypatch.setattr(manager_render, "MANAGER_TEMPLATE_VERSION", current_version)
    new = build_kit(model, previous_manifest=previous, previous_files=files)
    assert new.status == "ready"
    assert new.manifest.changes == {
        "added": (),
        "changed": (),
        "removed": (),
        "reasons": ("изменилась версия шаблонов доставки",),
    }
    repeat = build_kit(model, previous_manifest=new.manifest, previous_files=new.files)
    assert repeat.status == "unchanged" and repeat.files == new.files


@pytest.mark.parametrize("input_change", ["evidence", "project"])
def test_review_changes_recomputed_when_decision_hash_is_same(input_change):
    model = golden_model()
    old = build_kit(model)
    kwargs = {"form_evidence": {"Интерфейс 2": True}} if input_change == "evidence" else {}
    if input_change == "project":
        model = replace(model, project_id="another-project")
    new = build_kit(model, previous_manifest=old.manifest, previous_files=old.files, **kwargs)
    assert new.status == "ready" and new.manifest.decision_hash == old.manifest.decision_hash
    assert new.manifest.changes == {"added": (), "changed": (), "removed": ()}
    repeat = build_kit(model, previous_manifest=new.manifest, previous_files=new.files, **kwargs)
    assert repeat.status == "unchanged" and repeat.files == new.files


@pytest.mark.parametrize("method", REQUIRED_ROUTINES)
@pytest.mark.parametrize("mode", ["preserve", "canonical"])
def test_review_required_method_under_client_guard_refused(method, mode):
    text = DATA.read_text("utf-8-sig")
    routine = next(r for r in read_manager_text(text).routines if r.name == method)
    text = text.replace(
        routine.raw_text, "#Если Клиент Тогда\n" + routine.raw_text + "\n#КонецЕсли"
    )
    model = import_manager(
        read_manager_text(text), project_id="guarded", manager_name="PilotManager"
    )[0]
    with pytest.raises(AuthoringPreconditionError) as error:
        render_manager_kit(
            model,
            render(model, mode),
            host_metadata(),
            ManagerRoute(PLAN, "1.20"),
            executor_profile_id=PROFILE,
        )
    failure = error.value.failures[0]
    assert failure.id == "ed.author.model_invalid"
    assert failure.address == "Код/" + method and method in failure.message


@pytest.mark.parametrize(
    "branch",
    [
        "#Если Сервер Тогда",
        "#Если Не Сервер Тогда",
        "#Если Сервер Тогда\n#Иначе",
        "#Если Сервер Тогда\n#ИначеЕсли Клиент Тогда",
        "#Если ТолькоЗаголовки Тогда",
        "#Если УсловиеПримененияВыполняется Тогда",
    ],
)
def test_review_other_method_guard_refused(branch):
    text = DATA.read_text("utf-8-sig")
    routine = next(r for r in read_manager_text(text).routines if r.name == "ПередКонвертацией")
    text = text.replace(routine.raw_text, branch + "\n" + routine.raw_text + "\n#КонецЕсли")
    model = import_manager(
        read_manager_text(text), project_id="guarded", manager_name="PilotManager"
    )[0]
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(model)
    assert error.value.failures[0].id == "ed.author.model_invalid"
    assert error.value.failures[0].address == "Код/ПередКонвертацией"


@pytest.mark.parametrize("nested", [False, True])
def test_review_standard_whole_module_guard(nested):
    text = DATA.read_text("utf-8-sig")
    if nested:
        routine = next(r for r in read_manager_text(text).routines if r.name == "ПередКонвертацией")
        text = text.replace(
            routine.raw_text, "#Если Клиент Тогда\n" + routine.raw_text + "\n#КонецЕсли"
        )
    text = (
        "// #Если Клиент Тогда — комментарий\n"
        "#Если Сервер Или ТолстыйКлиентОбычноеПриложение Или ВнешнееСоединение Тогда\n"
        + text
        + "\n#КонецЕсли\n"
    )
    model = import_manager(
        read_manager_text(text), project_id="guarded", manager_name="PilotManager"
    )[0]
    if nested:
        with pytest.raises(AuthoringPreconditionError) as error:
            build_kit(model)
        assert error.value.failures[0].address == "Код/ПередКонвертацией"
    else:
        assert build_kit(model).status == "ready"


@pytest.mark.parametrize(
    "key",
    [
        "1.20\t",
        "1.20\u200b",
        "1.20 ",
        " 1.20",
        "1,20",
        'a"b',
        "1.20\x01",
        "1.20\u00a0",
        "1.20\u202e",
        "1.20\ufeff",
        "1.20\u115f",
        "1.20\u3164",
    ],
)
def test_review_invalid_route_key_is_typed_and_escaped(key):
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(route=ManagerRoute(PLAN, key))
    failure = error.value.failures[0]
    assert failure.id == "ed.author.route_scope_conflict"
    assert ascii(key) in failure.message and failure.address


def test_review_route_key_must_match_model_bindings():
    model = replace(golden_model(), format_bindings=(FormatBinding("1.7.Имя", "urn:test", "hash"),))
    with pytest.raises(AuthoringPreconditionError) as error:
        build_kit(model)
    failure = error.value.failures[0]
    assert failure.id == "ed.author.route_scope_conflict"
    assert ascii("1.20") in failure.message and ascii("1.7.Имя") in failure.message
    assert build_kit(model, route=ManagerRoute(PLAN, "1.7.Имя")).status == "ready"
    assert build_kit(route=ManagerRoute(PLAN, "1.7.Имя-test_2")).status == "ready"


@pytest.mark.parametrize("kind", ["module", "prefixed-module", "extension"])
@pytest.mark.parametrize("length", [80, 81, 200])
def test_review_delivery_name_length(kind, length):
    host = host_metadata()
    name = "М" * (length - len(host.identity.prefix))
    if kind == "prefixed-module":
        name = host.identity.prefix + name
    elif kind == "extension":
        host = replace(host, identity=replace(host.identity, name="Р" * length))
        name = "МенеджерОбмена"
    model = new_manager(manager_name=name)
    if length <= 80:
        assert build_kit(model, host=host).status == "ready"
    else:
        with pytest.raises(AuthoringPreconditionError) as error:
            build_kit(model, host=host)
        failure = error.value.failures[0]
        assert failure.id == "ed.author.identifier_conflict"
        assert "80" in failure.message and failure.address


def test_review_instruction_substitutions_without_editing_template(monkeypatch):
    from kd2_rules_mcp.authoring.ed import instruction, manager_render

    commands = (
        "create_infobase",
        "load_extension",
        "check_extension",
        "check_modules",
        "update_extension",
        "load_build_extension",
        "dump_extension",
    )
    template = "\n".join(
        "${" + key + "}"
        for key in (
            "compatibility_mode",
            "interface_compatibility_mode",
            "module_name_literal",
            *("command_" + key for key in commands),
        )
    )

    class TemplateResource:
        def joinpath(self, path):
            assert path == "templates/manager_instruction.md"
            return self

        def read_text(self, encoding):
            assert encoding == "utf-8"
            return template

        def read_bytes(self):
            return template.encode()

    monkeypatch.setattr(instruction, "files", lambda _: TemplateResource())
    monkeypatch.setattr(manager_render, "files", lambda _: TemplateResource())
    kit = build_kit(host=read_manager_host(host_descriptions("Version8_3_24", "Taxi"), PLAN))
    lines = kit.instruction.splitlines()
    assert lines[:3] == ["Version8_3_24", "Taxi", '"' + kit.module_name + '"']
    assert len(lines[3:]) == len(commands)
    for command in lines[3:]:
        assert "/Out " in command and "/DumpResult " in command
        assert "/DisableStartupDialogs" in command and "..." not in command
    assert '/LoadConfigFromFiles "<каталог комплекта>/extension"' in lines[4]
    for command in lines[4:]:
        assert '-Extension "' + kit.manifest.identity.name + '"' in command
