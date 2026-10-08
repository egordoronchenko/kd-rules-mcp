"""Комплект B: профиль §1.2, независимые байтовые эталоны и владение результатом."""

import hashlib
import re
import sqlite3
import subprocess
import zipfile
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from lxml import etree

from kd_rules_mcp.authoring.ed import ArtifactManifest, IdentityMap, render_authoring
from kd_rules_mcp.authoring.ed.identity import (
    EXTENSION_IDENTITY_SEED,
    artifact_uuid,
    identity_map_from_xml,
    make_identity_map,
)
from kd_rules_mcp.authoring.ed.model import (
    AttributeDraft,
    AuthoringPreconditionError,
    ExtensionIdentity,
    SourceSet,
    digest,
)
from kd_rules_mcp.authoring.ed.xml_dump import V8, XR, XSI, M, profile_template
from kd_rules_mcp.structures import db, xmlbuild, xmldump
from kd_rules_mcp.validation.ed_authoring import prepare_authoring
from kd_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
from tests.test_ed_authoring_hook import whitelist, with_test_fields
from tests.test_ed_authoring_model import DATA, IDENTITY, OPERATION, TARGET, inputs

BASE = DATA / "base/descriptions"
CASES = ("catalog-string", "existing", "document-string", "boolean", "number", "date")


def descriptions():
    return {p.relative_to(BASE).as_posix(): p.read_text("utf-8") for p in BASE.rglob("*.xml")}


def stable_inputs(version=2):
    value = inputs(version)
    # Читатель передаёт готовые отпечатки; golden не зависит от абсолютного cwd.
    sources = SourceSet(
        "Пример",
        "Main",
        digest(tuple(f.sha256 for f in value.document.files)),
        digest(
            tuple(
                (p.name, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted((DATA / "base").glob("*.bin"))
            )
        ),
        hashlib.sha256((DATA / "base/structure.xml").read_bytes()).hexdigest(),
        digest(value.routes),
        (),
        digest({}),
    )
    return replace(value, source_set=sources, input_fingerprints=sources)


def case_operation(case):
    if case == "existing":
        return replace(OPERATION, configuration_attribute="Заметка", new_attribute=None)
    if case == "document-string":
        return replace(OPERATION, target=replace(TARGET, pko_address="ПКО/Заказ"))
    if case == "catalog-string":
        return OPERATION
    primitive, prop, quals = {
        "boolean": ("boolean", "Булево", {}),
        "number": (
            "number",
            "Число",
            {"number_length": 10, "number_precision": 2, "number_nonnegative": True},
        ),
        "date": ("date", "Дата", {"date_parts": "DateTime"}),
    }[case]
    name = "доп_" + prop
    return replace(
        OPERATION,
        configuration_attribute=name,
        format_property=prop,
        new_attribute=AttributeDraft(name, prop, primitive, quals),
    )


def prepared(case="catalog-string", *, version=2, operations=None, identity=IDENTITY):
    return prepare_authoring(
        stable_inputs(version),
        operations or (case_operation(case),),
        identity,
        version_scope="manager",
    )


def xml(bundle, path):
    return etree.fromstring(bundle.files["extension/" + path])


@pytest.mark.parametrize("case", CASES)
def test_render_golden_and_repeat(case):
    value = prepared(case)
    bundle = render_authoring(value, descriptions())
    expected_dir = DATA / "expected" / case
    expected = {
        p.relative_to(expected_dir).as_posix(): p.read_bytes()
        for p in expected_dir.rglob("*")
        if p.is_file()
    }
    assert bundle.files == expected
    assert "Проверьте данные перед первым обменом" not in bundle.instruction
    assert b"ed.schema.required_unfilled" not in bundle.files["validation.json"]
    assert bundle.files == render_authoring(value, descriptions()).files
    count = 4 if case == "existing" else 5
    assert len([p for p in bundle.files if p.startswith("extension/")]) == count
    assert ArtifactManifest.from_bytes(bundle.files["manifest.json"]) == bundle.manifest
    assert not bundle.manifest.runtime_verified
    assert bundle.manifest.unverified
    for name, content in bundle.files.items():
        assert (
            content.endswith(b"\n")
            and b"\r" not in content
            and not content.startswith(b"\xef\xbb\xbf")
        )
        assert b"C:\\" not in content and b"C:/" not in content
        assert not Path(name).is_absolute() and ".." not in name.split("/")
    if case == "existing":
        assert not any("Catalogs/" in p for p in bundle.files)


def test_profile_configuration_language_module_and_owner_provenance():
    identity = ExtensionIdentity("доп_Обмен", "доп_", 'Обмен < & " >', "0.2", "Version8_3_21")
    bundle = render_authoring(prepared(identity=identity), descriptions())
    config = xml(bundle, "Configuration.xml")
    assert config.get("version") == "2.20"
    assert list(config.nsmap) == [
        None,
        "app",
        "cfg",
        "cmi",
        "ent",
        "lf",
        "style",
        "sys",
        "v8",
        "v8ui",
        "web",
        "win",
        "xen",
        "xpr",
        "xr",
        "xs",
        "xsi",
    ]
    obj = config[0]
    assert obj.get("uuid") != "00000000-0000-0000-0000-000000000001"
    contained = obj.findall(f"{{{M}}}InternalInfo/{{{XR}}}ContainedObject")
    assert [c.findtext(f"{{{XR}}}ClassId") for c in contained] == [
        "9cd510cd-abfc-11d4-9434-004095e12fc7",
        "9fcd25a0-4822-11d4-9414-008048da11f9",
        "e3687481-0a87-462c-a166-9f34594f9bba",
        "9de14907-ec23-4a07-96f0-85521cb6b53b",
        "51f2d5d8-ea4d-4064-8892-82951750031e",
        "e68182ea-4237-4383-967f-90c1e3370bc7",
        "fb282519-d103-4dd3-bc12-cb271d631dfc",
    ]
    props = obj.find(f"{{{M}}}Properties")
    assert props is not None
    assert {etree.QName(n).localname: n.text for n in props if len(n) == 0} == {
        "ObjectBelonging": "Adopted",
        "Name": "доп_Обмен",
        "Comment": None,
        "ConfigurationExtensionPurpose": "Customization",
        "KeepMappingToExtendedConfigurationObjectsByIDs": "true",
        "NamePrefix": "доп_",
        "ConfigurationExtensionCompatibilityMode": "Version8_3_21",
        "DefaultRunMode": "ManagedApplication",
        "ScriptVariant": "Russian",
        "DefaultRoles": None,
        "Vendor": None,
        "Version": "0.2",
        "DefaultLanguage": "Language.Русский",
        "BriefInformation": None,
        "DetailedInformation": None,
        "Copyright": None,
        "VendorInformationAddress": None,
        "ConfigurationInformationAddress": None,
        "InterfaceCompatibilityMode": "TaxiEnableVersion8_2",
    }
    assert props.findtext(f"{{{M}}}Synonym/{{{V8}}}item/{{{V8}}}content") == identity.synonym
    purpose = props.find(f"{{{M}}}UsePurposes/{{{V8}}}Value")
    assert (
        purpose is not None
        and purpose.text == "PlatformApplication"
        and purpose.get(f"{{{XSI}}}type") == "app:ApplicationUsePurpose"
    )
    child_objects = obj.find(f"{{{M}}}ChildObjects")
    assert child_objects is not None
    assert [(etree.QName(n).localname, n.text) for n in child_objects] == [
        ("Language", "Русский"),
        ("CommonModule", "Менеджер2"),
        ("Catalog", "Товары"),
    ]
    language = xml(bundle, "Languages/Русский.xml")[0]
    assert language.findtext(f"{{{M}}}Properties/{{{M}}}Name") == "Русский"
    assert language.findtext(f"{{{M}}}Properties/{{{M}}}LanguageCode") == "ru"
    assert (
        language.findtext(f"{{{M}}}Properties/{{{M}}}ExtendedConfigurationObject")
        == "00000000-0000-0000-0000-000000000002"
    )
    module = xml(bundle, "CommonModules/Менеджер2.xml")[0]
    assert (
        module.findtext(f"{{{M}}}Properties/{{{M}}}ExtendedConfigurationObject")
        == "00000000-0000-0000-0000-000000000012"
    )
    assert (
        module.findtext(f"{{{M}}}InternalInfo/{{{XR}}}PropertyState/{{{XR}}}Property") == "Module"
    )
    assert module.findtext(f"{{{M}}}InternalInfo/{{{XR}}}PropertyState/{{{XR}}}State") == "Extended"
    flags = {
        k: module.findtext(f"{{{M}}}Properties/{{{M}}}{k}")
        for k in (
            "Global",
            "ClientManagedApplication",
            "Server",
            "ExternalConnection",
            "ClientOrdinaryApplication",
            "ServerCall",
        )
    }
    assert flags == {
        "Global": "false",
        "ClientManagedApplication": "false",
        "Server": "true",
        "ExternalConnection": "true",
        "ClientOrdinaryApplication": "true",
        "ServerCall": "false",
    }
    owner = xml(bundle, "Catalogs/Товары.xml")[0]
    assert (
        owner.findtext(f"{{{M}}}Properties/{{{M}}}ExtendedConfigurationObject")
        == "00000000-0000-0000-0000-000000000003"
    )
    generated = owner.findall(f"{{{M}}}InternalInfo/{{{XR}}}GeneratedType")
    assert [(g.get("name"), g.get("category")) for g in generated] == [
        ("Catalog" + c + ".Товары", c) for c in ("Object", "Ref", "Selection", "List", "Manager")
    ]
    assert all(
        g.findtext(f"{{{XR}}}{r}") != "00000000-0000-0000-0000-000000000090"
        for g in generated
        for r in ("TypeId", "ValueId")
    )
    for file in (
        "Configuration.xml",
        "Languages/Русский.xml",
        "CommonModules/Менеджер2.xml",
        "Catalogs/Товары.xml",
    ):
        assert (
            xml(bundle, file)[0].findtext(f"{{{M}}}Properties/{{{M}}}ObjectBelonging") == "Adopted"
        )
    assert b"ReturnValuesReuse" not in bundle.files["extension/CommonModules/Менеджер2.xml"]


@pytest.mark.parametrize("case", [c for c in CASES if c != "existing"])
def test_attribute_closed_properties_and_round_trip(case, tmp_path):
    value = prepared(case)
    bundle = render_authoring(value, descriptions())
    kind, owner = ("Document", "Заказ") if case == "document-string" else ("Catalog", "Товары")
    relative = f"{kind}s/{owner}.xml"
    root = xml(bundle, relative)
    attr = root.find(f"{{{M}}}{kind}/{{{M}}}ChildObjects/{{{M}}}Attribute")
    assert attr is not None and list(attr.attrib) == ["uuid"]
    props = attr.find(f"{{{M}}}Properties")
    assert props is not None
    names = [etree.QName(n).localname for n in props]
    expected = [
        "Name",
        "Synonym",
        "Comment",
        "Type",
        "PasswordMode",
        "Format",
        "EditFormat",
        "ToolTip",
        "MarkNegatives",
        "Mask",
        "MultiLine",
        "ExtendedEdit",
        "MinValue",
        "MaxValue",
        "FillFromFillingValue",
        "FillValue",
        "FillChecking",
        "ChoiceFoldersAndItems",
        "ChoiceParameterLinks",
        "ChoiceParameters",
        "QuickChoice",
        "CreateOnInput",
        "ChoiceForm",
        "LinkByType",
        "ChoiceHistoryOnInput",
        "Indexing",
        "FullTextSearch",
        "DataHistory",
    ]
    if kind == "Catalog":
        expected.insert(25, "Use")
    assert names == expected
    fill = props.find(f"{{{M}}}FillValue")
    assert fill is not None
    if case == "date":
        assert fill.get(f"{{{XSI}}}nil") == "true" and fill.text is None
    else:
        primitive = {"number": "decimal", "boolean": "boolean"}.get(case, "string")
        assert fill.get(f"{{{XSI}}}type") == "xs:" + primitive
        assert fill.text == {"number": "0", "boolean": "false"}.get(case)
    for tag in ("MinValue", "MaxValue"):
        bound = props.find(f"{{{M}}}{tag}")
        assert bound is not None and bound.get(f"{{{XSI}}}nil") == "true"
    for name, content in bundle.files.items():
        if name.startswith("extension/"):
            destination = tmp_path / name.removeprefix("extension/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    path = tmp_path / relative
    read = xmldump.read_object(path, kind)
    assert len(read.attributes) == 1
    draft = value.operations[0].new_attribute
    assert draft is not None
    field = read.attributes[0]
    assert field.name == draft.name and field.synonym == draft.synonym
    typ = field.type
    assert typ.entries == [
        (
            "Type",
            {"string": "string", "boolean": "boolean", "number": "decimal", "date": "dateTime"}[
                draft.primitive
            ],
        )
    ]
    assert {
        "string": typ.string_length,
        "boolean": None,
        "number": typ.number_length,
        "date": typ.date_parts,
    }[draft.primitive] == {
        "catalog-string": 150,
        "document-string": 150,
        "number": 10,
        "boolean": None,
        "date": "DateTime",
    }[case]
    if case == "number":
        assert typ.number_precision == 2 and typ.number_nonnegative
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(db.SCHEMA)
        xmlbuild.build(xmlbuild.Metadata(xmldump.read_dump(tmp_path)), connection)
        snapshot = StructureSnapshot.load(connection)
    loaded = snapshot.objects[
        ("документ" if kind == "Document" else "справочник", owner.casefold())
    ].property(draft.name)[0]
    projected = value.structure_after.objects[
        ("документ" if kind == "Document" else "справочник", owner.casefold())
    ].property(draft.name)[0]
    assert loaded.types == projected.types
    for key, qualifier in draft.qualifiers.items():
        actual = loaded.qualifiers[key]
        assert actual == qualifier or (
            key == "date_parts" and actual == "Дата и время" and qualifier == "DateTime"
        )


@pytest.mark.parametrize(
    "primitive,property_name,qualifiers",
    [
        ("string", "Комментарий", {"string_length": 0}),
        (
            "number",
            "Число",
            {"number_length": 10, "number_precision": 0, "number_nonnegative": False},
        ),
        ("date", "День", {"date_parts": "Date"}),
        ("date", "Время", {"date_parts": "Time"}),
        ("date", "День", {"date_parts": "Дата"}),
        ("date", "Время", {"date_parts": "Время"}),
    ],
)
def test_all_qualifier_variants(primitive, property_name, qualifiers):
    op = replace(
        OPERATION,
        format_property=property_name,
        new_attribute=AttributeDraft(
            OPERATION.configuration_attribute, "Поле", primitive, qualifiers
        ),
    )
    value = prepared(operations=(op,))
    typ = xml(render_authoring(value, descriptions()), "Catalogs/Товары.xml").find(
        f".//{{{M}}}Type"
    )
    read = xmldump.read_type(typ)
    if primitive == "string":
        assert read.string_length == 0 and not read.string_fixed
    elif primitive == "number":
        assert (
            read.number_length == 10 and read.number_precision == 0 and not read.number_nonnegative
        )
    else:
        assert read.date_parts == {"Дата": "Date", "Время": "Time"}.get(
            str(qualifiers["date_parts"]), qualifiers["date_parts"]
        )


def test_extension_identity_uuids_stay_pinned():
    """УИД расширения и его объекта зафиксированы: зерно не зависит от имени пакета."""
    config = "00000000-0000-0000-0000-000000000001"
    name = "ДоработкаОбмена"
    key = "Catalog/Товары/Attribute/доп_Заметка"
    assert artifact_uuid(config, name) == "a0e1c48a-a154-59a6-a136-d4371d2a1cc6"
    mapped = make_identity_map(config, name, (key,), {})
    assert mapped.objects["catalog/товары/attribute/доп_заметка"] == (
        "e7f7215f-71f0-5277-b950-106a86d3c5b6"
    )


def test_uuid_formula_augmentation_subset_and_manifest_round_trip():
    first = render_authoring(prepared(), descriptions())
    expected_namespace = uuid5(
        NAMESPACE_URL,
        EXTENSION_IDENTITY_SEED + "00000000-0000-0000-0000-000000000001/доработкаобмена",
    )
    assert artifact_uuid("00000000-0000-0000-0000-000000000001", IDENTITY.name) == str(
        expected_namespace
    )
    assert first.manifest.identity_map.objects["catalog/товары/attribute/доп_заметка"] == str(
        uuid5(expected_namespace, "catalog/товары/attribute/доп_заметка")
    )
    second_op = replace(
        OPERATION,
        format_property="ВнешнийКод",
        configuration_attribute="доп_Код",
        new_attribute=AttributeDraft("доп_Код", "Код", "string", {"string_length": 20}),
    )
    second = render_authoring(
        prepared(operations=(second_op,)),
        descriptions(),
        previous_manifest=ArtifactManifest.from_bytes(first.files["manifest.json"]),
        previous_files=first.files,
    )
    assert len(second.prepared.operations) == 2
    assert all(
        second.manifest.identity_map.objects[k] == v
        for k, v in first.manifest.identity_map.objects.items()
    )
    assert (
        second.files
        == render_authoring(prepared(operations=(second_op, OPERATION)), descriptions()).files
    )
    subset = render_authoring(
        prepared(), descriptions(), previous_manifest=second.manifest, previous_files=second.files
    )
    assert subset.status == "unchanged" and subset.files == second.files
    hook = second.files["modules/CommonModules/Менеджер2/Ext/Module.bsl"].decode()
    assert hook.count("&После") == 1 and hook.count("ДобавитьПКС(") == 2
    assert len(xml(second, "Catalogs/Товары.xml").findall(f".//{{{M}}}Attribute")) == 2


@pytest.mark.parametrize("damage", ["edit", "missing", "extra", "manifest", "identity_map"])
def test_owned_content_changed(damage):
    bundle = render_authoring(prepared(), descriptions())
    actual = dict(bundle.files)
    manifest = bundle.manifest
    if damage == "edit":
        actual["extension/Catalogs/Товары.xml"] += b"<!-- edit -->\n"
    elif damage == "missing":
        actual.pop("instruction.md")
    elif damage == "extra":
        actual["unknown.txt"] = b"extra\n"
    elif damage == "manifest":
        actual["manifest.json"] += b" "
    else:
        mapping = dict(manifest.identity_map.objects)
        mapping["configuration"] = "00000000-0000-0000-0000-000000000999"
        manifest = replace(
            manifest,
            identity_map=IdentityMap(
                manifest.identity_map.artifact_uuid, mapping, manifest.identity_map.borrowed
            ),
        )
        actual["manifest.json"] = manifest.to_bytes()
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(
            prepared(), descriptions(), previous_manifest=manifest, previous_files=actual
        )
    assert error.value.failures[0].id == "ed.author.owned_content_changed"
    assert bundle.files["manifest.json"] == bundle.manifest.to_bytes()


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "invalid_content"])
def test_previous_artifact_names_damaged_manifest(damage):
    from kd_rules_mcp.authoring.ed.artifacts import previous_artifact

    bundle = render_authoring(prepared(), descriptions())
    actual = dict(bundle.files)
    if damage == "missing":
        actual.pop("manifest.json")
    else:
        actual["manifest.json"] = b"{" if damage == "invalid_json" else b"{}"
    with pytest.raises(AuthoringPreconditionError) as error:
        previous_artifact(actual)
    failure = error.value.failures[0]
    assert failure.id == "ed.author.owned_content_changed"
    assert "manifest.json" in failure.message


def test_union_conflict_preserves_old_files():
    first = render_authoring(prepared(), descriptions())
    assert OPERATION.new_attribute is not None
    conflict = replace(
        OPERATION,
        configuration_attribute="доп_ДругаяЗаметка",
        new_attribute=replace(OPERATION.new_attribute, name="доп_ДругаяЗаметка"),
    )
    before = dict(first.files)
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(
            prepared(operations=(conflict,)),
            descriptions(),
            previous_manifest=first.manifest,
            previous_files=first.files,
        )
    assert "ed.author.format_property_occupied" in {f.id for f in error.value.failures}
    assert first.files == before


@pytest.mark.parametrize(
    "path,change",
    [
        ("Configuration.xml", "version"),
        ("Languages/Русский.xml", "missing"),
        ("CommonModules/Менеджер2.xml", "flag"),
        ("Catalogs/Товары.xml", "generated"),
        ("Configuration.xml", "mode"),
        ("Configuration.xml", "compatibility"),
        ("Configuration.xml", "doctype"),
        ("Configuration.xml", "broken"),
    ],
)
def test_profile_refusals(path, change):
    texts = descriptions()
    if change == "missing":
        del texts[path]
    else:
        text = texts[path]
        edits = {
            "version": ('version="2.20"', 'version="2.19"'),
            "flag": ("<Server>true</Server>", "<Server>Unknown</Server>"),
            "generated": ('category="Ref"', 'category="Object"'),
            "mode": ("ManagedApplication", "OrdinaryApplication"),
            "compatibility": ("Version8_3_27", "Version8_3_19"),
            "doctype": (
                "<MetaDataObject",
                '<!DOCTYPE MetaDataObject [<!ENTITY x "unsafe">]>\n<MetaDataObject',
            ),
            "broken": ("</MetaDataObject>", ""),
        }
        texts[path] = text.replace(*edits[change])
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(prepared(), texts)
    assert error.value.failures[0].id == "ed.author.metadata_profile_unsupported"
    assert error.value.failures[0].file == path


@pytest.mark.parametrize("delivery", ["extension", "manual"])
@pytest.mark.parametrize("version", [1, 2, 3])
def test_instruction_modes_complete_neutral_and_runtime_status(delivery, version):
    value = prepared(version=version)
    bundle = render_authoring(value, descriptions(), delivery=delivery)
    text = bundle.instruction
    assert "${" not in text and "[if " not in text and "[/if]" not in text
    assert "extension_with_load" not in text and "sandbox_install_command" not in text
    assert "Не проверено живым обменом" in text
    assert TARGET.project not in text and not re.search(r"[A-Za-z]:[\\/]", text)
    assert all(s.reason in text for s in value.skipped)
    if delivery == "manual":
        assert "## Ручное внесение" in text and "## Установка готовой выгрузки" not in text
        assert not any(p.startswith("extension/") for p in bundle.files)
        insertion = text.split("Вставка в тело существующего перехватчика:", 1)[1].split("```", 2)[
            1
        ]
        assert "Процедура " not in insertion and "&После" not in insertion
        assert "ДобавитьПКС" in insertion and "второй не создавайте" in text
        assert (
            bundle.manifest.identity_map
            == render_authoring(value, descriptions()).manifest.identity_map
        )
    else:
        assert "## Установка готовой выгрузки" in text and "## Ручное внесение" not in text
        assert (
            bundle.files["modules/CommonModules/Менеджер" + str(version) + "/Ext/Module.bsl"]
            == bundle.files["extension/CommonModules/Менеджер" + str(version) + "/Ext/Module.bsl"]
        )


def test_empty_tables_are_explicit():
    bundle = render_authoring(prepared("existing"), descriptions())
    assert "Создаваемые реквизиты:\nНет\n" in bundle.instruction


@pytest.mark.parametrize("delivery", ["extension", "manual"])
@pytest.mark.parametrize("direction", ["send", "receive"])
@pytest.mark.parametrize("version", [2, 3])
def test_instruction_stand_risks_safe_mode_probes_and_spacing(delivery, direction, version):
    op = replace(OPERATION, target=replace(TARGET, direction=direction))
    value = prepared(version=version, operations=(op,))
    bundle = render_authoring(value, descriptions(), delivery=delivery)
    text = bundle.instruction
    assert "безопасный режим" in text and "администратор" in text
    assert "ЗащитаОтОпасныхДействий.ПредупреждатьОбОпасныхДействиях = Ложь" in text
    assert "Другое расширение может добавить ПКС" in text
    assert "имя реквизита фактически найденной ПКС" in text
    assert "Если найдена ПКС чужого реквизита" in text
    assert ("У интерфейса 3 проход ТолькоЗаголовки" in text) == (version == 3)
    assert "\n\n\n" not in text
    assert ("во входящем сообщении" in text) == (direction == "receive")
    assert ("неразличимы" in text) == (direction == "receive")
    if delivery == "extension":
        commands = [line for line in text.splitlines() if line.startswith("1cv8 DESIGNER ")]
        assert len(commands) == 4
        for key, command in zip(
            (
                "/LoadConfigFromFiles",
                "/CheckCanApplyConfigurationExtensions",
                "/CheckModules",
                "/UpdateDBCfg",
            ),
            commands,
            strict=True,
        ):
            assert key in command
            assert '/IBConnectionString "<строка соединения>"' in command
            assert '-Extension "ДоработкаОбмена"' in command
            assert "/N " not in command and "/P " not in command
        assert "-Format Hierarchical" in commands[0]
        assert "-Server -ExternalConnection" in commands[2]
    probes = text.split("## Проверка в базе", 1)[1].split("Ожидаемый результат:", 1)[0]
    # Обе стороны печатают фактическую пару и ищут реквизит даже при отсутствии ПКО.
    assert probes.count("Правило.Свойства.Количество()") == 2
    assert probes.count('Строка(ПКС.СвойствоКонфигурации) + " ↔ "') == 4
    assert probes.count("Для Каждого ПКО Из Правила Цикл") == 2
    assert probes.count("Если Правило = Неопределено Тогда") == 2
    assert "Отправка: " in probes and "Получение: " in probes
    from kd_rules_mcp.ed.lexer import lex
    from kd_rules_mcp.ed.model import SourceFile

    for code in re.findall(r"```bsl\n(.*?)\n```", probes, re.S):
        offsets = (0, *(i + 1 for i, char in enumerate(code) if char == "\n"))
        source = SourceFile("probe", "probe.bsl", code, digest(code), offsets)
        assert not lex(source).warnings


def test_instruction_real_uri_and_explained_route_states():
    value = prepared()
    source = value.preparation_inputs
    assert source is not None
    uri = "http://v8.1c.ru/edi/edi-format/1.20"
    schema = source.schemas["1.20"]
    assert not isinstance(schema, str)
    schema = replace(
        schema,
        base_namespace=uri,
        packages=tuple(
            replace(p, namespace=uri) if p.namespace == schema.base_namespace else p
            for p in schema.packages
        ),
    )
    plan = source.routes.plans[0]
    unreachable = replace(plan.entries[0], state="unreachable")
    routes = replace(source.routes, plans=(replace(plan, entries=(*plan.entries, unreachable)),))
    value = replace(
        value,
        preparation_inputs=replace(
            source, schemas={**source.schemas, "1.20": schema}, routes=routes
        ),
    )
    text = render_authoring(value, descriptions()).instruction
    assert "URI: " + uri + "." in text
    assert "htt[исходный файл]" not in text
    assert "| effective | Действующая запись карты |" in text
    assert "| unreachable | Недостижимая ветвь; менеджер по ней не вызывается |" in text
    assert "Пояснение" in text


def xml_except_version(payload: bytes) -> bytes:
    """Равенство выгрузки без свойства версии расширения."""
    return re.sub(rb"<Version>[^<]*</Version>", b"<Version></Version>", payload, count=1)


def test_v1_kit_plus_handler_keeps_object_uuids_and_rejects_tamper():
    """Переход на форму с обработчиками — явный просмотр; UUID объектов остаются."""
    from kd_rules_mcp.authoring.ed.artifacts import previous_artifact
    from kd_rules_mcp.authoring.ed.model import PreserveMissingHeaderProperty
    from kd_rules_mcp.authoring.ed.render import render_handlers_authoring
    from kd_rules_mcp.validation.ed_authoring import prepare_handler_operations
    from tests.test_ed_authoring_handlers import handler_inputs

    op = replace(OPERATION, target=replace(TARGET, direction="receive"))
    # Менеджер первого среза без диспетчера не проходит предусловие обработчика.
    value = prepare_authoring(handler_inputs(), (op,), IDENTITY, version_scope="manager")
    assert value.preparation_inputs is not None
    first = render_authoring(value, descriptions())
    assert first.manifest.schema_version == 1
    prop = first.manifest.operations[0]
    preserve = PreserveMissingHeaderProperty(prop.target, prop.operation_id)
    plan = prepare_handler_operations(
        value.preparation_inputs, (prop, preserve), IDENTITY, version_scope="manager"
    )
    migrated = render_handlers_authoring(
        value.preparation_inputs,
        plan,
        IDENTITY,
        descriptions(),
        previous_manifest=first.manifest,
        previous_files=first.files,
    )
    fresh = render_handlers_authoring(value.preparation_inputs, plan, IDENTITY, descriptions())
    assert migrated.manifest.schema_version == 2
    assert migrated.manifest.generator_version == "ed-authoring/2"
    assert migrated.manifest.identity_map == first.manifest.identity_map
    assert migrated.manifest.procedures
    assert all("runtime_verified" in item for item in migrated.manifest.handler_bindings)
    for path, payload in first.files.items():
        if not path.endswith(".xml"):
            continue
        if path == "extension/Configuration.xml":
            assert xml_except_version(migrated.files[path]) == xml_except_version(payload)
            assert migrated.files[path] != payload
            assert migrated.manifest.extension_version.encode() in migrated.files[path]
            assert migrated.manifest.extension_version == plan.decision_hash[:12]
            assert migrated.manifest.identity.version == first.manifest.identity.version
        else:
            assert migrated.files[path] == payload
    module = next(
        path for path in migrated.files if path.startswith("modules/") and path.endswith(".bsl")
    )
    text = migrated.files[module].decode("utf-8")
    assert '&Вместо("ВыполнитьПроцедуруМодуляМенеджера")' in text
    assert "ПередЗаписьюПолученныхДанных" in text
    assert 'ДобавитьПКС(Правило.Свойства, "доп_Заметка", "Комментарий");' in text
    assert "Пример" not in migrated.instruction
    assert "добавлен обработчик события" in migrated.instruction
    assert "получает\n   безопасный режим — снимите его" in migrated.instruction
    assert (
        "при обновлении существующего расширения режим сохраняется прежним" in migrated.instruction
    )
    assert fresh.files == migrated.files
    repeated = render_handlers_authoring(
        value.preparation_inputs,
        plan,
        IDENTITY,
        descriptions(),
        previous_manifest=migrated.manifest,
        previous_files=migrated.files,
    )
    assert repeated.status == "unchanged"
    assert repeated.files == migrated.files
    assert previous_artifact(migrated.files) == migrated.manifest
    damaged = dict(migrated.files)
    damaged[module] = damaged[module].replace(
        "\t\tВозврат;".encode(), "\t\tВозврат; // edit".encode(), 1
    )
    with pytest.raises(AuthoringPreconditionError) as error:
        previous_artifact(damaged)
    assert error.value.failures[0].id == "ed.author.owned_content_changed"
    forged = replace(
        migrated.manifest,
        procedures=tuple(
            {**dict(item), "body_sha256": "0" * 64} if item.get("role") == "handler" else dict(item)
            for item in migrated.manifest.procedures
        ),
    )
    forged_files = dict(migrated.files)
    forged_files["manifest.json"] = forged.to_bytes()
    with pytest.raises(AuthoringPreconditionError) as error:
        previous_artifact(forged_files)
    assert error.value.failures[0].id == "ed.author.owned_content_changed"


def test_handler_only_xml_matches_v1_on_an_existing_attribute():
    """XML комплекта без прямых ПКС совпадает с v1 на существующем реквизите."""
    from kd_rules_mcp.authoring.ed.render import render_handlers_authoring
    from kd_rules_mcp.validation.ed_authoring import prepare_handler_operations
    from tests.test_ed_authoring_handlers import handler, handler_inputs

    value = handler_inputs()
    existing = replace(OPERATION, configuration_attribute="Заметка", new_attribute=None)
    first = render_authoring(
        prepare_authoring(value, (existing,), IDENTITY, version_scope="manager"), descriptions()
    )
    plan = prepare_handler_operations(value, (handler(),), IDENTITY, version_scope="manager")
    only = render_handlers_authoring(value, plan, IDENTITY, descriptions())
    # По смыслу ни одного файла нет лишь в одном комплекте: оба без нового реквизита.
    assert sorted(set(first.files) - set(only.files)) == []
    assert sorted(set(only.files) - set(first.files)) == []
    differ = sorted(path for path, payload in only.files.items() if first.files[path] != payload)
    assert differ == [
        "extension/CommonModules/Менеджер2/Ext/Module.bsl",
        "extension/Configuration.xml",
        "instruction.md",
        "manifest.json",
        "modules/CommonModules/Менеджер2/Ext/Module.bsl",
        "validation.json",
    ]
    for path, payload in first.files.items():
        if not path.endswith(".xml"):
            continue
        if path == "extension/Configuration.xml":
            assert xml_except_version(only.files[path]) == xml_except_version(payload)
            assert only.manifest.extension_version == plan.decision_hash[:12]
        else:
            assert only.files[path] == payload


def test_direct_only_plan_is_refused_and_empty_preparation_stays_closed():
    from kd_rules_mcp.authoring.ed.render import render_handlers_authoring
    from kd_rules_mcp.validation.ed_authoring import prepare_handler_operations
    from tests.test_ed_authoring_handlers import handler_inputs

    value = handler_inputs()
    plan = prepare_handler_operations(value, (OPERATION,), IDENTITY, version_scope="manager")
    with pytest.raises(AuthoringPreconditionError) as error:
        render_handlers_authoring(value, plan, IDENTITY, descriptions())
    assert error.value.failures[0].id == "ed.author.unprepared_operations"
    assert (
        error.value.failures[0].message
        == "комплект только из прямых ПКС собирается `render_authoring`"
    )
    with pytest.raises(ValueError, match="хотя бы одна операция"):
        prepare_authoring(value, (), IDENTITY, version_scope="manager")
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(replace(prepared(), operations=()), descriptions())
    assert error.value.failures[0].id == "ed.author.unprepared_operations"


def test_version_two_adds_and_drops_an_operation_only_when_named():
    from kd_rules_mcp.authoring.ed.render import render_handlers_authoring
    from kd_rules_mcp.validation.ed_authoring import prepare_handler_operations
    from tests.test_ed_authoring_handlers import handler, handler_inputs

    value = handler_inputs()
    send = handler()
    receive = handler("ПередЗаписьюПолученныхДанных")
    first_plan = prepare_handler_operations(value, (send,), IDENTITY, version_scope="manager")
    first = render_handlers_authoring(value, first_plan, IDENTITY, descriptions())
    second_plan = prepare_handler_operations(
        value, (send, receive), IDENTITY, version_scope="manager"
    )
    second = render_handlers_authoring(
        value,
        second_plan,
        IDENTITY,
        descriptions(),
        previous_manifest=first.manifest,
        previous_files=first.files,
    )
    assert second.status == "ready"
    assert second.manifest.schema_version == 2
    assert second.manifest.identity_map == first.manifest.identity_map
    for path, payload in first.files.items():
        if not path.endswith(".xml"):
            continue
        if path == "extension/Configuration.xml":
            assert xml_except_version(second.files[path]) == xml_except_version(payload)
            assert second.manifest.extension_version != first.manifest.extension_version
        else:
            assert second.files[path] == payload
    added = {op.operation_id for op in second.manifest.handler_operations} - {
        op.operation_id for op in first.manifest.handler_operations
    }
    assert len(added) == 1
    added_id = next(iter(added))
    with pytest.raises(AuthoringPreconditionError) as error:
        render_handlers_authoring(
            value,
            first_plan,
            IDENTITY,
            descriptions(),
            previous_manifest=second.manifest,
            previous_files=second.files,
        )
    assert error.value.failures[0].id == "ed.author.owned_content_changed"
    assert added_id in error.value.failures[0].message
    dropped = render_handlers_authoring(
        value,
        first_plan,
        IDENTITY,
        descriptions(),
        previous_manifest=second.manifest,
        previous_files=second.files,
        drop_operations=frozenset({added_id}),
    )
    assert dropped.files == first.files
    with pytest.raises(AuthoringPreconditionError) as error:
        render_handlers_authoring(
            value,
            first_plan,
            IDENTITY,
            descriptions(),
            previous_manifest=second.manifest,
            previous_files=second.files,
            drop_operations=frozenset({"нет-такой-операции"}),
        )
    assert error.value.failures[0].id == "ed.author.unprepared_operations"


def test_receive_instruction_golden():
    op = replace(OPERATION, target=replace(TARGET, direction="receive"))
    text = render_authoring(prepared(operations=(op,)), descriptions()).instruction
    assert text.encode("utf-8") == (DATA / "expected/receive-instruction.md").read_bytes()


@pytest.mark.parametrize("count", [1, 2, 100])
def test_many_properties_one_hook_and_stable_xml(count):
    ops = tuple(
        replace(
            OPERATION,
            format_property=f"Поле{i:03}",
            configuration_attribute=f"доп_Поле{i:03}",
            new_attribute=None,
        )
        for i in range(count)
    )
    value = with_test_fields(stable_inputs(), ops)
    p = prepare_authoring(value, ops, IDENTITY, version_scope="manager")
    bundle = render_authoring(p, descriptions())
    assert len(whitelist(p.generated_hook.source, "send")[1]) == count
    assert bundle.files == render_authoring(p, descriptions()).files
    assert len(bundle.manifest.identity_map.objects) == 10


def test_external_identity_map_matches_every_xml_role():
    value = prepared()
    bundle = render_authoring(value, descriptions())
    texts = {
        p.removeprefix("extension/"): b.decode()
        for p, b in bundle.files.items()
        if p.endswith(".xml")
    }
    identity = identity_map_from_xml(texts, bundle.manifest.base_configuration_uuid, IDENTITY.name)
    assert identity == bundle.manifest.identity_map
    assert render_authoring(value, descriptions(), identity_map=identity).files == bundle.files
    mapping = dict(identity.objects)
    mapping["language/русский"] = "00000000-0000-0000-0000-000000000777"
    external = IdentityMap(identity.artifact_uuid, mapping, identity.borrowed)
    overridden = render_authoring(value, descriptions(), identity_map=external)
    assert xml(overridden, "Languages/Русский.xml")[0].get("uuid") == mapping["language/русский"]
    assert (
        xml(overridden, "Languages/Русский.xml")[0].findtext(
            f"{{{M}}}Properties/{{{M}}}ExtendedConfigurationObject"
        )
        == "00000000-0000-0000-0000-000000000002"
    )


def test_canonical_prepared_values_are_the_only_render_input():
    operation = replace(
        OPERATION,
        target=replace(TARGET, pko_address="пко/товар"),
        configuration_attribute="ЗАМЕТКА",
        format_property=" комментарий ",
        new_attribute=None,
    )
    bundle = render_authoring(prepared(operations=(operation,)), descriptions())
    canonical = render_authoring(prepared("existing"), descriptions())
    assert bundle.files == canonical.files
    assert bundle.prepared.operations[0].configuration_attribute == "Заметка"
    assert bundle.prepared.operations[0].format_property == "Комментарий"


@pytest.mark.parametrize("delivery", ["extension", "manual"])
def test_rerun_uses_all_previous_hashes(delivery):
    value = prepared()
    first = render_authoring(value, descriptions(), delivery=delivery)
    again = render_authoring(
        value,
        descriptions(),
        delivery=delivery,
        previous_manifest=first.manifest,
        previous_files=first.files,
    )
    assert again.status == "unchanged" and again.files == first.files
    assert set(first.manifest.file_hashes) == set(first.files) - {"manifest.json"}
    assert all(
        hashlib.sha256(first.files[p]).hexdigest() == h
        for p, h in first.manifest.file_hashes.items()
    )


def test_source_and_namespace_changes_end_ownership():
    value = prepared()
    first = render_authoring(value, descriptions())
    sources = descriptions()
    sources["Configuration.xml"] = (
        sources["Configuration.xml"].replace("Не копируется", "Новая редакция") + "\n"
    )
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(
            value, sources, previous_manifest=first.manifest, previous_files=first.files
        )
    assert error.value.failures[0].id == "ed.author.owned_content_changed"
    changed_map = replace(
        first.manifest.identity_map, artifact_uuid="00000000-0000-0000-0000-000000000888"
    )
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(value, descriptions(), identity_map=changed_map)
    assert error.value.failures[0].id == "ed.author.owned_content_changed"


def test_compatibility_default_and_explicit_override():
    sources = descriptions()
    default = render_authoring(prepared(), sources)
    assert (
        xml(default, "Configuration.xml").findtext(
            f".//{{{M}}}ConfigurationExtensionCompatibilityMode"
        )
        == "Version8_3_27"
    )
    sources["Configuration.xml"] = sources["Configuration.xml"].replace(
        "Version8_3_27", "Version8_3_19"
    )
    explicit = render_authoring(
        prepared(identity=replace(IDENTITY, compatibility_mode="Version8_3_24")), sources
    )
    assert (
        xml(explicit, "Configuration.xml").findtext(
            f".//{{{M}}}ConfigurationExtensionCompatibilityMode"
        )
        == "Version8_3_24"
    )


def test_changed_hook_is_rejected_in_working_path():
    value = prepared()
    hook = replace(
        value.generated_hook,
        source=replace(
            value.generated_hook.source,
            text=value.generated_hook.source.text + 'Сообщить("лишнее");\n',
        ),
    )
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(replace(value, generated_hook=hook), descriptions())
    assert error.value.failures[0].id == "ed.author.snapshot_mismatch"


def test_manual_still_checks_xml_and_requires_preparation_inputs():
    sources = descriptions()
    sources["Configuration.xml"] = sources["Configuration.xml"].replace(
        'version="2.20"', 'version="2.19"'
    )
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(prepared(), sources, delivery="manual")
    assert error.value.failures[0].id == "ed.author.metadata_profile_unsupported"
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(replace(prepared(), preparation_inputs=None), descriptions())
    assert error.value.failures[0].id == "ed.author.snapshot_mismatch"


def test_invalid_xml_characters_are_a_profile_refusal():
    with pytest.raises(AuthoringPreconditionError) as error:
        render_authoring(
            prepared(identity=replace(IDENTITY, synonym="Описание\x00")), descriptions()
        )
    assert error.value.failures[0].id == "ed.author.metadata_profile_unsupported"


def test_borrowed_uuid_is_copied_byte_for_byte():
    sources = descriptions()
    reference = "ABCDEF00-0000-0000-0000-000000000003"
    sources["Catalogs/Товары.xml"] = sources["Catalogs/Товары.xml"].replace(
        "00000000-0000-0000-0000-000000000003", reference
    )
    bundle = render_authoring(prepared(), sources)
    assert (
        xml(bundle, "Catalogs/Товары.xml").findtext(f".//{{{M}}}ExtendedConfigurationObject")
        == reference
    )
    assert bundle.manifest.identity_map.borrowed["catalog/товары"] == reference


def test_resources_in_built_wheel(tmp_path):
    assert profile_template()["profile"] == "xml-2.20-platform-8.3.27"
    resource = files("kd_rules_mcp.authoring.ed").joinpath("templates/instruction.md")
    assert "${operations_table}" in resource.read_text("utf-8")
    result = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--offline",
            "--no-create-gitignore",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        for name in ("instruction.md", "handlers_instruction.md", "xml_profile_2_20.json"):
            packaged = archive.read("kd_rules_mcp/authoring/ed/templates/" + name)
            assert (
                packaged
                == files("kd_rules_mcp.authoring.ed").joinpath("templates/" + name).read_bytes()
            )


def refresh_goldens() -> None:
    """Перезаписывает байтовые эталоны текущим рендером.

    УИДы комплекта считаются от замороженного зерна и при переименовании проекта
    не меняются. Вызывать, когда меняется сам рендер, а не имя пакета.
    """
    rendered = descriptions()
    for case in CASES:
        bundle = render_authoring(prepared(case), rendered)
        dest = DATA / "expected" / case
        for rel, content in bundle.files.items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
