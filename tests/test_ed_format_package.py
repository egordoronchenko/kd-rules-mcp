"""Синтетические проверки F1/F3 и команд платформенной пробы."""

import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from lxml import etree

from kd_rules_mcp.authoring.ed.format_package import (
    FORMAT_DECLARE_PROCEDURE,
    FORMAT_OVERRIDE_MODULE,
    FormatDeclaration,
    FormatFacet,
    FormatProperty,
    FormatType,
    format_package_from_schema,
    read_format_host,
    render_format_extension,
    render_format_package,
)
from kd_rules_mcp.authoring.ed.identity import make_identity_map
from kd_rules_mcp.authoring.ed.manager_render import ManagerRoute, render_manager_route
from kd_rules_mcp.authoring.ed.manifest import sha256
from kd_rules_mcp.authoring.ed.xml_dump import M, parse_xml, profile_template, read_description
from kd_rules_mcp.ed.layer_model import LayerDescriptor
from kd_rules_mcp.ed.layer_reader import read_extension_text
from kd_rules_mcp.ed.routes import read_routes
from kd_rules_mcp.ed.schema import QName, load_schema
from kd_rules_mcp.ed.schema.xdto import XDTO, XS
from kd_rules_mcp.errors import (
    EdFormatDuplicateNameError,
    EdFormatEmptyObjectError,
    EdFormatIdentifierError,
    EdFormatMissingKeyError,
    EdFormatNamespaceError,
    EdFormatShapeError,
    EdFormatUnknownTypeError,
)
from kdbase.ed_format_package_check import (
    DeclarationProbe,
    check_format_package,
    compare_format_extension,
    main,
    normalized_package_xml,
)
from tests.data.ed.format_package.model import BASE, OWN, sample_model

DATA = Path(__file__).parent / "data/ed/format_package"
HOST = Path(__file__).parent / "data/ed/registration/delivery/dump"


def write_files(root: Path, files: dict[str, bytes]) -> Path:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


def host():
    return read_format_host(
        {p: (HOST / p).read_text("utf-8") for p in ("Configuration.xml", "Languages/Russian.xml")}
    )


def extension_files():
    return render_format_extension(
        sample_model(), host(), extension_name="fmt_Extension", prefix="fmt_"
    )


def declaration_model(tmp_path: Path) -> FormatDeclaration:
    """Вымышленные исходники; карта читается тем же читателем, что настоящая выгрузка."""
    descriptions = {
        p: (HOST / p).read_text("utf-8")
        for p in ("Configuration.xml", "Languages/Russian.xml", "ExchangePlans/TargetPlan.xml")
    }
    descriptions["Configuration.xml"] = descriptions["Configuration.xml"].replace(
        "</ChildObjects>",
        f"<CommonModule>{FORMAT_OVERRIDE_MODULE}</CommonModule>"
        "<CommonModule>FictionManager</CommonModule></ChildObjects>",
    )
    flags = "".join(
        f"<{flag}>{'true' if flag in ('Server', 'ExternalConnection') else 'false'}</{flag}>"
        for flag in profile_template()["module_flags"]
    )
    for name in (FORMAT_OVERRIDE_MODULE, "FictionManager"):
        descriptions["CommonModules/" + name + ".xml"] = (
            f'<MetaDataObject xmlns="{M}" version="2.20">'
            '<CommonModule uuid="01b42451-e422-4e55-96e1-9d6b309eeb11">'
            f"<Properties><Name>{name}</Name>{flags}</Properties></CommonModule></MetaDataObject>"
        )
    descriptions["CommonModules/" + FORMAT_OVERRIDE_MODULE + "/Ext/Module.bsl"] = (
        f"Процедура {FORMAT_DECLARE_PROCEDURE}(РасширенияФормата) Экспорт\nКонецПроцедуры\n"
    )
    descriptions["CommonModules/FictionManager/Ext/Module.bsl"] = (
        'Функция ВерсияФорматаМенеджераОбмена() Экспорт\nВозврат "2";\nКонецФункции\n'
    )
    descriptions["ExchangePlans/TargetPlan/Ext/ManagerModule.bsl"] = (
        "Процедура ПриПолученииНастроек(НастройкиПлана) Экспорт\n"
        "НастройкиПлана.ЭтоПланОбменаXDTO = Истина;\n"
        'НастройкиПлана.ФорматОбмена = "http://v8.1c.ru/edi/edi_stnd/EnterpriseData";\n'
        "Версии = Новый Соответствие;\n"
        'Версии.Вставить("1.20", FictionManager);\n'
        "НастройкиПлана.ВерсииФорматаОбмена = Версии;\nКонецПроцедуры\n"
    )
    root = write_files(tmp_path / "host", {p: s.encode() for p, s in descriptions.items()})
    return FormatDeclaration("1.20", descriptions, read_routes(root), "TargetPlan")


def declared_files(declaration: FormatDeclaration):
    return render_format_extension(
        sample_model(),
        read_format_host(declaration.descriptions),
        extension_name="fmt_Extension",
        prefix="fmt_",
        declaration=declaration,
    )


def test_undeclared_carrier_bytes_unchanged():
    # Снимок F1/F3 до объявления: проверяет все файлы, включая порядок XML и UUID.
    assert {p: sha256(b) for p, b in extension_files().items()} == {
        "Configuration.xml": "e08708e790fbe44d3dd4542bb06018adbb6ac92a05018b3258f1e9192891a88b",
        "Languages/Russian.xml": "1ecdb76d1ff4823da970e5a2dc7e4bb35cc0ad271028545719cd932907812695",
        "XDTOPackages/fmt_Package.xml": (
            "5eb21217222df484d94553662710670b9a25a621097484a0b471af93a3c104d8"
        ),
        "XDTOPackages/fmt_Package/Ext/Package.bin": (
            "833258254e8bba44b468ac05faa905fac75ff81ecdb418cd47a2935d52818313"
        ),
    }


def test_declared_carrier_and_layer_reader(tmp_path: Path):
    declaration = declaration_model(tmp_path)
    files = declared_files(declaration)
    assert files == declared_files(declaration)
    assert all(b"\r" not in content for content in files.values())
    module_path = "CommonModules/" + FORMAT_OVERRIDE_MODULE
    module = read_description(
        module_path + ".xml", files[module_path + ".xml"].decode(), "CommonModule"
    )
    owner = read_description(
        module_path + ".xml", declaration.descriptions[module_path + ".xml"], "CommonModule"
    )
    assert module.props["ExtendedConfigurationObject"] == owner.uuid
    assert all(module.props[f] == owner.props[f] for f in profile_template()["module_flags"])
    xml = etree.fromstring(files[module_path + ".xml"])
    assert xml.xpath("//*[local-name()='PropertyState']/*[local-name()='State']/text()") == [
        "Extended"
    ]
    reading = read_extension_text(
        files[module_path + "/Ext/Module.bsl"].decode(),
        layer=LayerDescriptor("extension", 1, "fmt_Extension", "", None, ""),
        metadata_name=FORMAT_OVERRIDE_MODULE,
    )
    assert len(reading.hooks) == 1
    assert reading.hooks[0].target_name == FORMAT_DECLARE_PROCEDURE
    assert reading.hooks[0].kind == "after"
    plan_path = "ExchangePlans/TargetPlan"
    assert (
        "ПриПолученииНастроек(НастройкиПлана)"
        in files[plan_path + "/Ext/ManagerModule.bsl"].decode()
    )
    assert (
        files[plan_path + "/Ext/ManagerModule.bsl"].decode().count('&После("ПриПолученииНастроек")')
        == 1
    )
    assert OWN.encode() in files[plan_path + "/Ext/ManagerModule.bsl"]
    plan = read_description(plan_path + ".xml", files[plan_path + ".xml"].decode(), "ExchangePlan")
    assert (
        plan.props["ExtendedConfigurationObject"]
        == read_description(
            plan_path + ".xml", declaration.descriptions[plan_path + ".xml"], "ExchangePlan"
        ).uuid
    )
    extension = write_files(tmp_path / "extension", files)
    assert compare_format_extension(extension, extension)["equal"]
    without_plan = declared_files(replace(declaration, plan_name=None))
    assert not any(p.startswith("ExchangePlans/") for p in without_plan)
    assert module_path + "/Ext/Module.bsl" in without_plan


def test_combined_settings_hook():
    text = render_manager_route(
        ManagerRoute("TargetPlan", "1.20"),
        module_name="fmt_Manager",
        prefix="fmt_",
        format_namespace=OWN,
    )
    assert text.count('&После("ПриПолученииНастроек")') == 1
    assert 'ВерсииФорматаОбмена.Вставить("1.20", fmt_Manager);' in text
    assert f'РасширенияФорматаОбмена.Вставить("{OWN}", "1.20");' in text
    assert "РасширенияФорматаОбмена =" not in text


@pytest.mark.parametrize(
    "parameters",
    [
        "",
        "РасширенияФормата, Другой",
        "Знач РасширенияФормата",
        "РасширенияФормата = Неопределено",
        "Другой",
        "РасширенияФормата Другой",
    ],
)
def test_declaration_signature_refusals(tmp_path: Path, parameters: str):
    declaration = declaration_model(tmp_path)
    descriptions = dict(declaration.descriptions)
    descriptions["CommonModules/" + FORMAT_OVERRIDE_MODULE + "/Ext/Module.bsl"] = (
        f"Процедура {FORMAT_DECLARE_PROCEDURE}({parameters}) Экспорт\nКонецПроцедуры\n"
    )
    with pytest.raises(EdFormatShapeError, match="этой версии БСП"):
        declared_files(replace(declaration, descriptions=descriptions))


def test_declaration_missing_procedure_and_uri_collisions(tmp_path: Path):
    declaration = declaration_model(tmp_path)
    path = "CommonModules/" + FORMAT_OVERRIDE_MODULE + "/Ext/Module.bsl"
    for text, error in (
        ("Процедура Другая() Экспорт\nКонецПроцедуры\n", EdFormatShapeError),
        (
            declaration.descriptions[path].replace(
                "КонецПроцедуры", f'РасширенияФормата.Вставить("{OWN}", "1.20");\nКонецПроцедуры'
            ),
            EdFormatNamespaceError,
        ),
    ):
        with pytest.raises(error):
            declared_files(
                replace(declaration, descriptions={**declaration.descriptions, path: text})
            )
    with pytest.raises(EdFormatNamespaceError):
        declared_files(
            replace(declaration, extension_sources=(f'Расширения.Вставить("{OWN}", "1.20");',))
        )


@pytest.mark.parametrize("key", ["1.20.0", "1.20A", "1.20a", " 1.20 "])
def test_declaration_exact_plan_key(tmp_path: Path, key: str):
    declaration = declaration_model(tmp_path)
    plan = declaration.routes.plans[0]
    entry = replace(plan.entries[0], key=key.strip(), key_raw=key)
    routes = replace(declaration.routes, plans=(replace(plan, entries=(entry,)),))
    with pytest.raises(EdFormatShapeError, match="буква в букву"):
        declared_files(replace(declaration, routes=routes))
    with pytest.raises(EdFormatShapeError, match="буква в букву"):
        declared_files(replace(declaration, version_key=key))


def test_bytes_repeat_and_identity_map():
    model = sample_model()
    assert render_format_package(model) == render_format_package(model)
    assert extension_files() == extension_files()
    paths = ("XDTOPackage/" + model.metadata_name,)
    identity = make_identity_map(host().configuration_uuid, "fmt_Extension", paths, {})
    rendered = render_format_package(model, identity=identity)
    description = read_description(
        "package.xml", rendered["XDTOPackages/fmt_Package.xml"].decode(), "XDTOPackage"
    )
    assert description.uuid == identity.objects["xdtopackage/fmt_package"]
    with pytest.raises(FrozenInstanceError):
        attribute = "namespace"
        setattr(model, attribute, "urn:changed")
    assert all(b"\r" not in content for content in rendered.values())


def test_roundtrip_all_declared_values(tmp_path: Path):
    model = sample_model()
    write_files(tmp_path, render_format_package(model))
    schema = load_schema(DATA / "base.bin", extensions=(tmp_path / "XDTOPackages/fmt_Package.xml",))
    assert schema.status == "complete"
    assert not schema.diagnostics
    actual = format_package_from_schema(
        schema,
        namespace=OWN,
        metadata_name=model.metadata_name,
        base_version=model.base_version,
        base_namespace=BASE,
        roles={t.name: t.role for t in model.types},
        exported={t.name: t.key_property for t in model.types if t.exported and t.key_property},
    )
    assert actual == model
    # Сверяем и наличие явно заданных атрибутов, которое не участвует в равенстве модели.
    assert render_format_package(actual) == render_format_package(model)
    assert schema.types[QName(OWN, "ItemRows")].properties[0].upper is None


@pytest.mark.parametrize("local", ["", "1Name", "Bad Name", "Bad:Name", "../Name", "{urn:x}Name"])
def test_invalid_type_name(local: str):
    with pytest.raises(EdFormatIdentifierError):
        FormatType(QName(OWN, local), "reference", base=QName(BASE, "Ref"))


def test_type_ncname_allows_platform_dotted_names():
    assert FormatType(QName(OWN, "СправочникСсылка.Тест"), "reference", base=QName(BASE, "Ref"))


def test_invalid_metadata_name_and_property_name():
    with pytest.raises(EdFormatIdentifierError):
        replace(sample_model(), metadata_name="Bad/Name")
    with pytest.raises(EdFormatIdentifierError):
        FormatProperty(QName(OWN, "Bad Name"), QName(XS, "string"))


def test_duplicate_type_and_property():
    model = sample_model()
    with pytest.raises(EdFormatDuplicateNameError):
        replace(model, types=(*model.types, model.types[0]))
    prop = FormatProperty(QName(OWN, "Text"), QName(XS, "string"))
    with pytest.raises(EdFormatDuplicateNameError):
        FormatType(QName(OWN, "Duplicate"), "row", (prop, prop))


@pytest.mark.parametrize(
    "typ",
    [
        QName(OWN, "Missing"),
        QName(BASE, "Missing"),
        QName("urn:absent", "ExternalRef"),
        QName(XS, "missing"),
    ],
)
def test_unresolved_property_type(typ: QName):
    model = sample_model()
    row = replace(model.types[3], properties=(FormatProperty(QName(OWN, "Text"), typ),))
    with pytest.raises(EdFormatUnknownTypeError):
        replace(model, types=(*model.types[:3], row, *model.types[4:]))


def test_unresolved_base_type():
    model = sample_model()
    with pytest.raises(EdFormatUnknownTypeError):
        replace(
            model, types=(replace(model.types[0], base=QName(BASE, "Missing")), *model.types[1:])
        )


def test_namespaces_and_missing_import():
    model = sample_model()
    with pytest.raises(EdFormatNamespaceError):
        replace(model, namespace=BASE)
    with pytest.raises(EdFormatNamespaceError):
        replace(model, types=(replace(model.types[0], name=QName(BASE, "Other")),))
    with pytest.raises(EdFormatNamespaceError):
        replace(model, imports=(BASE,))
    with pytest.raises(EdFormatUnknownTypeError):
        replace(model, imports=("urn:missing",))


def test_empty_object_and_missing_key():
    with pytest.raises(EdFormatEmptyObjectError):
        FormatType(QName(OWN, "Empty"), "object")
    model = sample_model()
    obj = model.types[-1]
    with pytest.raises(EdFormatMissingKeyError):
        replace(obj, key_property=None)
    with pytest.raises(EdFormatMissingKeyError):
        replace(model, types=(*model.types[:-1], replace(obj, properties=obj.properties[1:])))
    wrong = replace(obj.properties[0], type=QName(OWN, "Color"))
    with pytest.raises(EdFormatMissingKeyError):
        replace(
            model, types=(*model.types[:-1], replace(obj, properties=(wrong, *obj.properties[1:])))
        )


@pytest.mark.parametrize(("lower", "upper"), [(-1, 1), (2, 1), (0, -1)])
def test_invalid_bounds(lower: int, upper: int):
    with pytest.raises(EdFormatShapeError):
        FormatProperty(QName(OWN, "Bad"), QName(XS, "string"), lower, upper)


def test_invalid_value_declaration():
    with pytest.raises(EdFormatShapeError):
        FormatType(QName(OWN, "Enum"), "enumeration", base=QName(XS, "string"))
    with pytest.raises(EdFormatShapeError):
        FormatFacet("unsupported", "value")


def test_empty_version_and_unsupported_shape():
    model = sample_model()
    with pytest.raises(EdFormatShapeError):
        replace(model, base_version="")
    with pytest.raises(EdFormatShapeError):
        FormatType(QName(OWN, "Union"), "value", variety="union")
    with pytest.raises(EdFormatShapeError):
        FormatProperty(QName(OWN, "Broken"), QName(XS, "string"), form="unknown")  # type: ignore[arg-type]
    with pytest.raises(EdFormatShapeError):
        FormatProperty(QName(OWN, "Broken"), QName(XS, "string"), lower=1.5)  # type: ignore[arg-type]
    with pytest.raises(EdFormatShapeError):
        read_format_host({})


def test_base_key_is_accepted():
    model = sample_model()
    obj = model.types[-1]
    key = replace(obj.properties[0], type=QName(BASE, "Object"))
    assert replace(
        model, types=(*model.types[:-1], replace(obj, properties=(key, *obj.properties[1:])))
    )


def test_schema_import_rejects_incomplete_and_untyped_declarations(tmp_path: Path):
    raw = (
        f'<package xmlns="{XDTO}" xmlns:xs="{XS}" targetNamespace="{OWN}">'
        f'<import namespace="{BASE}"/><objectType name="Partial" unsupported="true">'
        '<property name="Text" type="xs:string"/></objectType></package>'
    ).encode()
    file = tmp_path / "unsupported.bin"
    file.write_bytes(raw)
    schema = load_schema(DATA / "base.bin", extensions=(file,))
    with pytest.raises(EdFormatShapeError):
        format_package_from_schema(
            schema,
            namespace=OWN,
            metadata_name="fmt_Package",
            base_version="1.20",
            base_namespace=BASE,
        )
    file.write_bytes(raw.replace(b' unsupported="true"', b"").replace(b' type="xs:string"', b""))
    schema = load_schema(DATA / "base.bin", extensions=(file,))
    with pytest.raises(EdFormatShapeError):
        format_package_from_schema(
            schema,
            namespace=OWN,
            metadata_name="fmt_Package",
            base_version="1.20",
            base_namespace=BASE,
        )


def test_extension_profile_and_composition():
    files = extension_files()
    assert set(files) == {
        "Configuration.xml",
        "Languages/Russian.xml",
        "XDTOPackages/fmt_Package.xml",
        "XDTOPackages/fmt_Package/Ext/Package.bin",
    }
    for path, content in files.items():
        if path.endswith(".xml"):
            root = parse_xml(path, content.decode())
            assert list(root.nsmap.values()) == [v for _, v in profile_template()["namespaces"]]
    config = read_description(
        "Configuration.xml", files["Configuration.xml"].decode(), "Configuration"
    )
    assert config.props["ConfigurationExtensionCompatibilityMode"] == host().compatibility_mode
    assert config.props["InterfaceCompatibilityMode"] == host().interface_compatibility_mode
    root = etree.fromstring(files["Configuration.xml"])
    children = root.find(f"{{{M}}}Configuration/{{{M}}}ChildObjects")
    assert children is not None
    assert [etree.QName(c).localname for c in children] == ["Language", "XDTOPackage"]
    language = read_description("language.xml", files["Languages/Russian.xml"].decode(), "Language")
    assert language.props["ExtendedConfigurationObject"] == host().language.uuid
    for mode in ("Version8_3_21", "Version8_3_27"):
        other = replace(host(), compatibility_mode=mode, interface_compatibility_mode="Taxi")
        content = render_format_extension(
            sample_model(), other, extension_name="fmt_Extension", prefix="fmt_"
        )
        assert mode.encode() in content["Configuration.xml"]
        assert b">Taxi<" in content["Configuration.xml"]


def test_extension_rejects_missing_modes_and_prefix():
    with pytest.raises(EdFormatShapeError):
        render_format_extension(
            sample_model(),
            replace(host(), compatibility_mode=""),
            extension_name="fmt_Extension",
            prefix="fmt_",
        )
    with pytest.raises(EdFormatIdentifierError):
        render_format_extension(sample_model(), host(), extension_name="Other", prefix="other_")


def test_package_normalization_ignores_prefixes_not_structure():
    raw = render_format_package(sample_model())["XDTOPackages/fmt_Package/Ext/Package.bin"]
    changed_prefixes = raw.replace(b"d3p1", b"renamed").replace(b"\t", b"   ")
    assert normalized_package_xml(raw) == normalized_package_xml(changed_prefixes)
    for before, after in [
        (b'lowerBound="0"', b'lowerBound="1"'),
        (b'nillable="true"', b'nillable="false"'),
        (b">Red<", b">Yellow<"),
        (b"urn:fiction:base", b"urn:other:base"),
    ]:
        assert normalized_package_xml(raw) != normalized_package_xml(raw.replace(before, after))
    root = etree.fromstring(raw)
    obj = root.find(f"{{{XDTO}}}objectType[@name='Item']")
    assert obj is not None
    obj.insert(0, obj[-1])
    assert normalized_package_xml(raw) != normalized_package_xml(etree.tostring(root))


@pytest.mark.parametrize(
    ("failure", "result", "mismatch", "expected", "count"),
    [
        ("", "0", False, 0, 5),
        ("/CheckConfig", "0", False, 1, 2),
        ("", "1", False, 1, 1),
        ("", "", False, 1, 1),
        ("", "0", True, 2, 5),
    ],
)
def test_probe_commands(
    tmp_path: Path, failure: str, result: str, mismatch: bool, expected: int, count: int
):
    base = tmp_path / "source"
    base.mkdir()
    (base / "1Cv8.1CD").write_bytes(b"fictional file base")
    extension = write_files(tmp_path / "extension", extension_files())
    calls: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> int:
        calls.append(argv)
        assert Path(argv[argv.index("/F") + 1]).is_relative_to(tmp_path / "run")
        assert "-Extension" in argv
        assert argv[argv.index("-Extension") + 1] == "fmt_Extension"
        if result:
            Path(argv[argv.index("/DumpResult") + 1]).write_text(result, "utf-8")
        if failure and failure in argv:
            return 7
        if "/DumpConfigToFiles" in argv:
            target = Path(argv[argv.index("/DumpConfigToFiles") + 1])
            shutil.copytree(extension, target, dirs_exist_ok=True)
            package = target / "XDTOPackages/fmt_Package/Ext/Package.bin"
            content = package.read_bytes().replace(b"d3p1", b"platformPrefix")
            if mismatch:
                content = content.replace(b">Red<", b">Yellow<")
            package.write_bytes(content)
        return 0

    code, report = check_format_package(
        base,
        extension,
        Path("1cv8.exe"),
        user="HiddenUser",
        run_root=tmp_path / "run",
        runner=runner,
    )
    assert code == expected
    assert len(calls) == count
    assert (base / "1Cv8.1CD").read_bytes() == b"fictional file base"
    assert not list((tmp_path / "run").iterdir())
    assert "HiddenUser" not in json.dumps(report)
    if expected != 1:
        assert report["compared_files"] == 4


def test_probe_launch_error_and_cli(tmp_path: Path):
    base = tmp_path / "source"
    base.mkdir()
    (base / "1Cv8.1CD").write_bytes(b"fictional file base")
    extension = write_files(tmp_path / "extension", extension_files())

    def runner(argv: Sequence[str]) -> int:
        raise PermissionError("Launch forbidden")

    code, report = check_format_package(
        base, extension, Path("1cv8.exe"), run_root=tmp_path / "run", runner=runner
    )
    assert code == 1 and report["error_type"] == "PermissionError"
    assert not list((tmp_path / "run").iterdir())
    assert (
        main(["--base", str(tmp_path), "--extension", str(extension), "--platform", "1cv8.exe"])
        == 1
    )


def test_probe_extra_file_and_unsafe_root(tmp_path: Path):
    expected = write_files(tmp_path / "extension", extension_files())
    actual = tmp_path / "dump"
    shutil.copytree(expected, actual)
    (actual / "Extra.xml").write_bytes(b"<unexpected/>")
    assert compare_format_extension(expected, actual)["mismatch_count"] == 1
    (actual / "1Cv8.1CD").write_bytes(b"fictional file base")
    assert check_format_package(actual, expected, Path("1cv8.exe"), run_root=actual / "run")[0] == 1


@pytest.mark.parametrize(
    "failure",
    [
        "",
        "activate",
        "uri_available",
        "type_found",
        "other_version_absent",
        "missing_result",
        "invalid_result",
    ],
)
def test_declaration_probe_second_stage(tmp_path: Path, failure: str):
    base = tmp_path / "source"
    base.mkdir()
    (base / "1Cv8.1CD").write_bytes(b"fictional file base")
    extension = write_files(tmp_path / "extension", declared_files(declaration_model(tmp_path)))
    probe = DeclarationProbe(OWN, "1.20", "Item", "1.20.0")
    commands: list[str] = []
    copies: list[Path] = []

    def runner(argv: Sequence[str]) -> int:
        if argv[0] == "cscript.exe":
            text = Path(argv[-1]).read_text("utf-16")
            command = json.loads(re.findall(r"var command = (.*);", text)[0])
            result = Path(json.loads(re.findall(r"var resultPath = (.*);", text)[0]))
            copies.append(Path(json.loads(re.findall(r"var basePath = (.*);", text)[0])))
            commands.append(command)
            assert copies[-1] != base and (copies[-1] / "1Cv8.1CD").is_file()
            assert 'new ActiveXObject("V83.COMConnector")' in text
            assert "found.БезопасныйРежим = false;" in text
            assert "found.ЗащитаОтОпасныхДействий.ПредупреждатьОбОпасныхДействиях = false;" in text
            assert "found.Записать();" in text
            assert "base.ОбменДаннымиXDTOСервер.ДоступныеРасширенияФормата(version)" in text
            assert "base.ОбменДаннымиXDTOСервер.ДоступныеРасширенияФормата(otherVersion)" in text
            assert "base.ФабрикаXDTO.Тип(uri, objectType)" in text
            assert "Запрос" not in text  # Прикладных таблиц/данных в этой пробе нет.
            values = (
                {"safe_mode_disabled": failure != "activate"}
                if command == "activate"
                else {
                    key: failure != key
                    for key in ("uri_available", "type_found", "other_version_absent")
                }
            )
            if command == "inspect" and failure == "missing_result":
                return 0
            if command == "inspect" and failure == "invalid_result":
                values = {"unexpected_private_data": True}
            result.write_text(json.dumps(values), "utf-8")
            return 0
        Path(argv[argv.index("/DumpResult") + 1]).write_text("0", "utf-8")
        if "/DumpConfigToFiles" in argv:
            target = Path(argv[argv.index("/DumpConfigToFiles") + 1])
            shutil.copytree(extension, target, dirs_exist_ok=True)
            for path in target.rglob("*.bsl"):
                path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
        return 0

    code, report = check_format_package(
        base,
        extension,
        Path("1cv8.exe"),
        user="HiddenUser",
        run_root=tmp_path / "run",
        runner=runner,
        declaration_probe=probe,
    )
    assert code == (0 if not failure else 1)
    assert report["runtime_declaration_verified"] is (not failure)
    assert report["runtime_exchange_verified"] is False
    assert commands == (["activate"] if failure == "activate" else ["activate", "inspect"])
    assert len(set(copies)) == 1
    assert not list((tmp_path / "run").iterdir())
    assert "HiddenUser" not in json.dumps(report)
    assert "unexpected_private_data" not in json.dumps(report)
    assert (base / "1Cv8.1CD").read_bytes() == b"fictional file base"


def test_declaration_cli_invalid_versions(capsys: pytest.CaptureFixture[str]):
    assert (
        main(
            [
                "--base",
                "fictional",
                "--extension",
                "fictional",
                "--platform",
                "fictional",
                "--version",
                "1.20",
                "--other-version",
                "1.20",
                "--user",
                "HiddenUser",
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["error_type"] == "ValueError"
    assert result["runtime_declaration_verified"] is False
    assert "HiddenUser" not in json.dumps(result)
