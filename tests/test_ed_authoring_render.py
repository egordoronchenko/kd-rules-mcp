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

from kd2_rules_mcp.authoring.ed import ArtifactManifest, IdentityMap, render_authoring
from kd2_rules_mcp.authoring.ed.identity import artifact_uuid, identity_map_from_xml
from kd2_rules_mcp.authoring.ed.model import (
    AttributeDraft,
    AuthoringPreconditionError,
    ExtensionIdentity,
    SourceSet,
    digest,
)
from kd2_rules_mcp.authoring.ed.xml_dump import V8, XR, XSI, M, profile_template
from kd2_rules_mcp.structures import db, xmlbuild, xmldump
from kd2_rules_mcp.validation.ed_authoring import prepare_authoring
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot
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


def test_uuid_formula_augmentation_subset_and_manifest_round_trip():
    first = render_authoring(prepared(), descriptions())
    expected_namespace = uuid5(
        NAMESPACE_URL,
        "kd2-rules-mcp/ed-authoring/v1/00000000-0000-0000-0000-000000000001/доработкаобмена",
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
    resource = files("kd2_rules_mcp.authoring.ed").joinpath("templates/instruction.md")
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
        for name in ("instruction.md", "xml_profile_2_20.json"):
            packaged = archive.read("kd2_rules_mcp/authoring/ed/templates/" + name)
            assert (
                packaged
                == files("kd2_rules_mcp.authoring.ed").joinpath("templates/" + name).read_bytes()
            )
