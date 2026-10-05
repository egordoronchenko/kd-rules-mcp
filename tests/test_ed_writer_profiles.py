"""Реестр исполнителя: доказательства, точный выбор и матрица интерфейсов на синтетике."""

import re
from dataclasses import replace
from itertools import product
from pathlib import Path

import pytest

from kd2_rules_mcp.authoring.ed.manager_operations import (
    IdentificationPatch,
    ManagerOperation,
    PkoPatch,
)
from kd2_rules_mcp.ed.executor_profile import (
    BSP_3_1_12_XDTO,
    detect_profile,
    detect_profile_text,
    executor_fingerprint,
)
from kd2_rules_mcp.ed.lexer import tokenize
from kd2_rules_mcp.ed.reader import read_manager_text
from kd2_rules_mcp.ed.writer import new_manager, render
from kd2_rules_mcp.ed.writer_import import import_manager, import_signature
from kd2_rules_mcp.ed.writer_model import EntityStyle, Value, dump_model, load_model, validate_model
from kd2_rules_mcp.validation.ed_writer import validate_writer
from tests.test_ed_writer import execute, pilot_model


def verified_detection():
    """Свои тела заменяют только отпечатки; таблица возможностей остаётся проверяемой."""
    profile = BSP_3_1_12_XDTO
    texts = {
        m.name: "\n".join(f"Процедура {p}()\nКонецПроцедуры\n" for p in m.procedures)
        for m in profile.modules
    }
    synthetic = replace(
        profile,
        modules=tuple(
            replace(m, sha256=executor_fingerprint(texts[m.name]), raw_sha256="")
            for m in profile.modules
        ),
    )
    return detect_profile_text(texts, profiles=(synthetic,)), texts, synthetic


def test_profile_normalization_and_manual_choice_do_not_hide_changes():
    detection, texts, profile = verified_detection()
    assert detection.verified and detection.profile == profile
    windows = {k: "\ufeff" + v.replace("\n", "\r\n") for k, v in texts.items()}
    assert detect_profile_text(windows, profiles=(profile,)).verified
    name = profile.modules[0].name
    changed = dict(texts, **{name: texts[name] + "// Ручная правка\n"})
    result = detect_profile_text(changed, profiles=(profile,))
    assert result.code == "executor_profile_unverified" and result.profile is None
    assert [(m.module, m.reason) for m in result.mismatches] == [(name, "fingerprint_mismatch")]
    manual = detect_profile_text(changed, requested_profile=profile.profile_id, profiles=(profile,))
    assert manual.profile == profile and not manual.verified
    assert manual.model_profile() == detection.model_profile()
    assert not manual.verified


def test_missing_procedure_and_module_are_named_without_nearest_profile():
    _, texts, profile = verified_detection()
    module = profile.modules[0]
    changed = dict(texts)
    changed[module.name] = changed[module.name].replace(module.procedures[0], "ДругаяПроцедура")
    result = detect_profile_text(changed, profiles=(profile,))
    assert {m.reason for m in result.mismatches} == {"fingerprint_mismatch", "missing_procedure"}
    assert (
        next(m.expected for m in result.mismatches if m.reason == "missing_procedure")
        == module.procedures[0]
    )
    result = detect_profile_text({}, profiles=(profile,))
    assert {m.module for m in result.mismatches} == {m.name for m in profile.modules}
    assert all(m.reason == "missing_module" for m in result.mismatches)
    result = detect_profile_text(texts, requested_profile="unknown", profiles=(profile,))
    assert result.mismatches[0].reason == "unknown_profile"


def test_filesystem_missing_executor(tmp_path):
    result = detect_profile(tmp_path)
    assert not result.verified
    assert all(m.reason == "missing_module" for m in result.mismatches)


@pytest.mark.parametrize("annotation", ["Вместо", "ИзменениеИКонтроль", "Перед", "После"])
@pytest.mark.parametrize("intercepted", [True, False])
def test_extensions_invalidate_only_executor_interceptions(
    tmp_path, monkeypatch, annotation, intercepted
):
    from kd2_rules_mcp.ed import executor_profile

    _, texts, profile = verified_detection()
    monkeypatch.setattr(executor_profile, "PROFILES", (profile,))
    base, extension = tmp_path / "base", tmp_path / "extension"
    for name, text in texts.items():
        path = base / "CommonModules" / name / "Ext/Module.bsl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    extension.mkdir()
    (extension / "Configuration.xml").write_text(
        "<MetaDataObject><Configuration><Properties><Name>TestExtension</Name></Properties>"
        "<ChildObjects/></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    module = profile.modules[0]
    name = module.procedures[0] if intercepted else "NotExecutorProcedure"
    path = extension / "CommonModules" / module.name / "Ext/Module.bsl"
    path.parent.mkdir(parents=True)
    path.write_text(
        f'&{annotation}("{name}")\nПроцедура Hook()\n'
        ' Сообщить("Не анализируется");\nКонецПроцедуры\n',
        encoding="utf-8",
    )
    result = detect_profile(base, [extension])
    assert result.verified != intercepted
    if intercepted:
        assert result.code == "executor_profile_unverified"
        assert result.mismatches[0].extension == "TestExtension"
        assert result.mismatches[0].procedures == (name,)
        assert result.mismatches[0].reason == "executor_intercepted"
    else:
        assert not result.mismatches


def test_snapshot_contains_selection_only_and_runtime_stays_in_registry():
    detection, _, _ = verified_detection()
    assert detection.profile is not None
    for interface, path in product((1, 2, 3), ("ordinary", "object")):
        selected = detection.model_profile(interface, path)
        assert selected.receive_mode == path
        assert ((interface, path) in detection.profile.runtime_verified) == (
            interface == 2 and path == "ordinary"
        )
        model = replace(new_manager(interface_version=interface), executor_profile=selected)
        assert load_model(dump_model(model)).executor_profile == selected


def test_legacy_snapshot_drops_proofs_and_migrates_revision():
    from kd2_rules_mcp.ed.writer_model import json_value

    model = load_model(
        (Path(__file__).parent / "data/ed/writer/legacy-profile.ed.json").read_bytes()
    )
    assert json_value(model.executor_profile) == {
        "profile_id": "bsp-3.1.12-xdto",
        "receive_mode": "",
    }
    assert load_model(dump_model(model)) == model
    report = validate_writer(model, render(model).data)
    assert [i.check for i in report.errors] == ["ed.writer.profile"]


@pytest.mark.parametrize(
    "interface,direction,path,headers",
    [
        (i, d, p, h)
        for i, d, p in product((1, 2, 3), ("send", "receive"), ("ordinary", "object"))
        for h in ((True, False) if i == 3 else (False,))
    ],
)
@pytest.mark.parametrize("minimal", [False, True])
def test_profile_matrix_signatures_calls_and_header_only_pass(
    interface, direction, path, headers, minimal
):
    detection, _, profile = verified_detection()
    model = pilot_model(interface) if minimal else new_manager(interface_version=interface)
    model = replace(model, executor_profile=detection.model_profile(interface, path))
    output = render(model)
    document = read_manager_text(output.data.decode("utf-8"))
    for contract in profile.contracts:
        if contract.required and interface in contract.interfaces:
            routine = next(r for r in document.routines if r.name == contract.name)
            assert import_signature(routine) == contract.signature
    for rule in (*document.pko, *document.pod):
        routine = next(r for r in document.routines if r.name == rule.procedure_name)
        key = "pod" if rule.kind == "pod" else "pko_v3" if interface == 3 else "pko_old"
        parameters = dict(profile.rule_parameters)[key]
        assert tuple(p.name for p in routine.parameters) == parameters
        uses = [
            u
            for u in document.rule_uses
            if u.rule_id == rule.entity_id and u.direction == direction
        ]
        original = next(
            r for r in (*model.pko, *model.pod) if r.procedure_name == rule.procedure_name
        )
        if direction in original.directions:
            assert uses
        for use in uses:
            tokens = tokenize(use.raw_text)
            assert tuple(t.value for t in tokens[2:-2] if t.kind == "identifier") == parameters
        if interface == 3 and rule.kind == "pko":
            body = document.files[0].text[routine.body_span.char_start : routine.body_span.char_end]
            guard = body.index("Если ТолькоЗаголовки Тогда")
            returned = body.index("Возврат;", guard)
            properties = body.index("СвойстваШапки =")
            assert guard < returned < properties
            # Статическая модель ветки: при Истина исполнение кончается до первой ПКС.
            visited = body[:returned] if headers else body
            assert ("ДобавитьПКС(" not in visited) == headers
    report = validate_writer(model, output.data, detection=detection, receive_path=path)
    assert not report.issues, [i.to_dict() for i in report.issues]
    assert import_manager(document, project_id="matrix")[0].header.interface_version == interface


def test_registry_has_only_evidenced_limits_and_invocations():
    profile = BSP_3_1_12_XDTO
    assert profile.name_limit("pko") is None and profile.name_limit("pod") is None
    assert profile.helper_has_namespace
    assert all(c.evidence for c in (*profile.contracts, *profile.columns, *profile.events))
    assert all(i.evidence for _, i in profile.invocations)
    assert dict(profile.invocations)["ПриУдаленииОбъектаИБ"].keys == (
        "ДанныеИБ",
        "УдалитьНепосредственно",
        "СтандартнаяОбработка",
    )
    search = next(e for e in profile.events if e.name == "АлгоритмПоиска")
    assert "АлгоритмПоиска:8697–8706" in search.evidence
    assert "внешний УИД и навигационная ссылка пусты" in search.object_condition
    assert "это не регистр" in search.object_condition
    assert "ДанныеИБ = Неопределено" in search.object_condition
    assert "СопоставитьОбъектПоПолямПоискаИлиАлгоритмом:2509–2544" in search.evidence
    deferred = next(e for e in profile.events if e.name == "ПослеЗагрузкиВсехДанных")
    assert "ЗапомнитьОбъектДляОтложенногоЗаполнения:7081–7091" in deferred.evidence
    clear = next(c for c in profile.columns if c.name == "ОчисткаДанных")
    assert "ВыгрузкаОбъектаВыборки:792–811" in clear.evidence
    hook = next(c for c in profile.contracts if c.name == "ПередОбработкойУдаляемогоОбъекта")
    assert not hook.required
    assert tuple(p.name for p in hook.signature.parameters) == ("КомпонентыОбмена", "Объект")
    assert "ПриУдаленииОбъекта:8744–8748" in hook.evidence


def test_imported_module_style_uses_modes_by_kind_direction_and_survives_reload():
    model = pilot_model()
    for name in ("Second", "Outlier"):
        model = execute(
            model,
            ManagerOperation(
                name,
                "pko",
                "create",
                patch=PkoPatch(
                    name=name,
                    directions=("receive",),
                    format_object=Value("string", "Catalog.Style"),
                ),
            ),
        )
    text = render(model).text.replace("\r\n", "\n")
    document = read_manager_text(text)
    for rule in reversed(document.pko):
        routine = next(r for r in document.routines if r.name == rule.procedure_name)
        start, end = routine.span.char_start, routine.span.char_end
        fragment = text[start:end]
        if rule.name != "Outlier":
            fragment = re.sub(
                r"(?m)^\t(ПравилоКонвертации\.\w+) + = ",
                lambda m: "  " + m[1].ljust(47) + " = ",
                fragment,
            )
            fragment = fragment.replace("\t", "  ")
            rows = fragment.split("\n")
            fragment = "\n".join((rows[0], "", "", *rows[1:]))
        text = text[:start] + fragment + text[end:]
    model = import_manager(read_manager_text(text), project_id="styles")[0]
    style = next(s for s in model.module_styles if (s.kind, s.direction) == ("pko", "receive"))
    assert style.assignment_width == 47 and style.indent == "  "
    assert style.opening_blank_lines == 2
    assert load_model(dump_model(model)).module_styles == model.module_styles
    changed = execute(
        model,
        ManagerOperation(
            "new",
            "pko",
            "create",
            patch=PkoPatch(
                name="New", directions=("receive",), format_object=Value("string", "Catalog.Style")
            ),
        ),
    )
    rule = next(r for r in changed.pko if r.name == "New")
    changed = execute(
        changed,
        ManagerOperation(
            "identify-new",
            "identification",
            "update",
            target_id=rule.identification.logical_id,
            patch=IdentificationPatch(mode=Value("string", "ПоУникальномуИдентификатору")),
        ),
    )
    output = render(changed, use_source_style=False)
    # Старый снимок не хранит моду; её восстановление не переписывает ревизию снимка.
    older = replace(changed, module_styles=())
    assert render(older, use_source_style=False).data == output.data
    entries = [e for e in output.report.entries if e.address.startswith("ПКО/New")]
    opening = next(e for e in entries if e.form == "opening")
    frame = output.data[opening.byte_start : opening.byte_end].decode("utf-8")
    assert frame.splitlines()[1:3] == ["", ""]
    for row in entries:
        fragment = output.data[row.byte_start : row.byte_end].decode("utf-8")
        for line in fragment.splitlines():
            if line.lstrip().startswith("ПравилоКонвертации.") and " = " in line:
                assert line.startswith("  ")
                assert len(line.split(" = ")[0].lstrip()) == 47


def test_new_manager_and_missing_samples_use_default_templates():
    model = pilot_model()
    assert not model.module_styles
    base = render(model).data
    imported = import_manager(read_manager_text(base.decode("utf-8")), project_id="defaults")[0]
    assert imported.module_styles
    assert (
        render(replace(imported, module_styles=()), use_source_style=False).data
        == render(imported, use_source_style=False).data
    )
    invalid = replace(model, module_styles=(EntityStyle("pko", "send", indent="bad"),))
    with pytest.raises(ValueError, match="стиль"):
        validate_model(invalid)
