"""Имена, владение и объединение проверенных комплектов без файловой системы."""

from dataclasses import replace

import pytest
from lxml import etree

from kd2_rules_mcp.authoring.ed.artifacts import artifact_name, combine_artifacts, previous_artifact
from kd2_rules_mcp.authoring.ed.model import AuthoringPreconditionError
from kd2_rules_mcp.authoring.ed.render import render_authoring
from kd2_rules_mcp.authoring.ed.xml_dump import M
from tests.test_ed_authoring_render import descriptions, prepared


def test_artifact_name_and_previous_ownership():
    assert (
        artifact_name("ДоработкаОбмена", "12345678-0000-0000-0000-000000000001")
        == "ДоработкаОбмена-12345678"
    )
    with pytest.raises(ValueError):
        artifact_name("../foreign", "12345678-0000-0000-0000-000000000001")
    assert previous_artifact({}) is None
    bundle = render_authoring(prepared("catalog-string"), descriptions())
    assert previous_artifact(bundle.files) == bundle.manifest
    for files in ({"foreign": b"x"}, {**bundle.files, "instruction.md": b"edit"}):
        with pytest.raises(AuthoringPreconditionError) as caught:
            previous_artifact(files)
        assert caught.value.failures[0].id == "ed.author.owned_content_changed"


def test_combine_managers_and_same_owner_attributes():
    from kd2_rules_mcp.validation.ed_authoring import prepare_authoring
    from tests.test_ed_authoring_model import IDENTITY
    from tests.test_ed_authoring_render import case_operation, stable_inputs

    first = render_authoring(prepared("catalog-string"), descriptions())
    second = render_authoring(
        prepare_authoring(
            stable_inputs(1), (case_operation("boolean"),), IDENTITY, version_scope="manager"
        ),
        descriptions(),
    )
    combined = combine_artifacts([first, second])
    assert combined.files == combine_artifacts([second, first]).files
    assert len([p for p in combined.files if p.startswith("modules/")]) == 2
    configuration = etree.fromstring(combined.files["extension/Configuration.xml"])
    assert configuration.xpath("count(//*[local-name()='CommonModule'])") == 2
    catalog = etree.fromstring(combined.files["extension/Catalogs/Товары.xml"])
    assert catalog.xpath("count(//*[local-name()='Attribute'])") == 2
    assert previous_artifact(combined.files) == combined.manifest
    assert combine_artifacts([first]).files == first.files
    conflict = replace(
        second,
        files={**second.files, "modules/CommonModules/Менеджер2/Ext/Module.bsl": b"different"},
    )
    with pytest.raises(AuthoringPreconditionError):
        combine_artifacts([first, conflict])
    assert catalog.find(f"{{{M}}}Catalog") is not None


def test_manual_combination_refuses_different_drafts_for_same_attribute():
    from kd2_rules_mcp.validation.ed_authoring import prepare_authoring
    from tests.test_ed_authoring_model import IDENTITY, OPERATION
    from tests.test_ed_authoring_render import stable_inputs

    assert OPERATION.new_attribute is not None
    changed = replace(
        OPERATION, new_attribute=replace(OPERATION.new_attribute, qualifiers={"string_length": 100})
    )
    first = render_authoring(prepared("catalog-string"), descriptions(), delivery="manual")
    second = render_authoring(
        prepare_authoring(stable_inputs(1), (changed,), IDENTITY, version_scope="manager"),
        descriptions(),
        delivery="manual",
    )
    with pytest.raises(AuthoringPreconditionError) as caught:
        combine_artifacts([first, second])
    assert caught.value.failures[0].id == "ed.author.identifier_conflict"


def test_combined_view_counts_actual_rules_and_shared_metadata():
    from kd2_rules_mcp.authoring.ed.model import Notice
    from kd2_rules_mcp.service.ed_authoring_views import build_view
    from tests.test_ed_authoring_model import OPERATION

    first = prepared(
        version=1,
        operations=(replace(OPERATION, target=replace(OPERATION.target, plan="ВторойПлан")),),
    )
    second = prepared(version=2)
    bundles = [render_authoring(p, descriptions()) for p in (first, second)]
    combined = combine_artifacts(bundles)
    operation = first.operations[0]
    first = replace(
        first,
        notices=(
            Notice(
                "ed.author.other_version_unverified",
                operation.operation_id,
                operation.target.pko_address,
                "Не прочитано",
                ("1.21",),
            ),
            Notice(
                "ed.author.other_version_incompatible",
                operation.operation_id,
                operation.target.pko_address,
                "Не совместимо",
                ("1.21",),
            ),
        ),
    )
    view = build_view(
        combined,
        [first, second],
        build_hash="test",
        status="ready",
        output_path="relative",
        written=False,
        offset=0,
        limit=200,
    )
    assert view["change_counts"] == {"pko_changed": 2, "pks_added": 2, "attributes_added": 1}
    assert view["validation"]["other_profiles"]["unverified"] == 1
