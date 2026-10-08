"""Повторное ревью D2: литералы диспетчера, локальные рамки и сиротский код."""

from dataclasses import replace

import pytest

from kd_rules_mcp.authoring.ed.manager_operations import (
    AlgorithmPatch,
    HandlerPatch,
    ManagerOperation,
    ManagerOperationError,
    PkoPatch,
    apply,
)
from kd_rules_mcp.ed.canonical import model_addresses
from kd_rules_mcp.ed.reader import read_manager_text
from kd_rules_mcp.ed.writer import render
from kd_rules_mcp.ed.writer_import import import_manager
from tests.test_ed_writer_code import execute, round_trip
from tests.test_ed_writer_code_review import base, branch_text, checks, imported, planned

OTHER = "ПКО_Other_ПриОтправкеДанных"
ITEM = "ПКО_Item_ПриОтправкеДанных"
DISPATCHER = "ВыполнитьПроцедуруМодуляМенеджера"


def test_n1_restore_refuses_literal_owned_by_another_method():
    source = base()
    original_branch = branch_text(source, OTHER)
    foreign_branch = original_branch[: original_branch.index("\n") + 1] + "\t\tCompute(1);\n"
    text = render(source).text.replace(original_branch, foreign_branch)
    model = imported(text)
    plan = planned(
        model,
        ManagerOperation(
            "restore",
            "handler",
            "update",
            target_id=model.pko[1].events[0].logical_id,
            patch=HandlerPatch(restore_dispatcher=True),
        ),
    )
    assert plan.failures
    assert any(
        OTHER in f.address + f.message
        and "Compute" in f.message
        and "строк" in f.message
        and f.references
        for f in plan.failures
    )
    assert plan.model == model and render(plan.model).text == text
    assert not checks(model)["ed.writer.dispatcher_literal_duplicate"]


def test_n1_restore_keeps_other_literal_for_the_same_method():
    source = base()
    text = render(source).text.replace(f'= "{OTHER}" Тогда', '= "Alias" Тогда')
    model = imported(text)
    alias = next(
        b.text for b in model.retained_blocks if b.kind == "dispatcher_case" and b.name == "Alias"
    )
    changed = execute(
        model,
        ManagerOperation(
            "restore",
            "handler",
            "update",
            target_id=model.pko[1].events[0].logical_id,
            patch=HandlerPatch(restore_dispatcher=True),
        ),
    )
    assert (
        next(
            b.text
            for b in changed.retained_blocks
            if b.kind == "dispatcher_case" and b.name == "Alias"
        )
        == alias
    )
    assert sum(c.name == OTHER for c in changed.dispatcher_cases) == 1
    assert not checks(changed)["ed.writer.dispatcher_literal_duplicate"]
    round_trip(changed)


@pytest.mark.parametrize("same_case", [True, False])
def test_n1_duplicate_literal_is_exact_and_scoped_to_dispatcher(same_case):
    text = render(base()).text
    text = text.replace(f'= "{OTHER}" Тогда', f'= "{ITEM if same_case else ITEM.lower()}" Тогда')
    model = imported(text)
    assert checks(model)["ed.writer.dispatcher_literal_duplicate"] == int(same_case)
    assert render(model).text == text
    round_trip(model)


def test_n1_same_literal_in_different_dispatchers_is_not_duplicate():
    source = execute(
        base(),
        ManagerOperation(
            "function",
            "algorithm",
            "create",
            patch=AlgorithmPatch(
                name="ComputeValue",
                routine_kind="function",
                parameters="Value",
                body="Возврат Value;",
            ),
        ),
    )
    text = render(source).text
    header = "Функция ВыполнитьФункциюМодуляМенеджера(ИмяФункции, Параметры) Экспорт\n"
    assert text.count(header) == 1
    text = text.replace(
        header,
        header
        + (f'\tЕсли ИмяФункции = "{ITEM}" Тогда\n\t\tВозврат ComputeValue(1);\n\tКонецЕсли;\n'),
    )
    assert not checks(imported(text))["ed.writer.dispatcher_literal_duplicate"]


def test_n1_duplicate_literal_in_empty_branch_is_detected():
    source = base()
    branch = branch_text(source, ITEM)
    text = render(source).text.replace(
        branch, branch + f'\tИначеЕсли ИмяПроцедуры = "{ITEM}" Тогда\n'
    )
    model = imported(text)
    assert checks(model)["ed.writer.dispatcher_literal_duplicate"] == 1
    assert render(model).text == text
    round_trip(model)


def test_n1_rule_rename_does_not_require_missing_dispatcher():
    text = render(base()).text
    routine = next(r for r in read_manager_text(text).routines if r.name == DISPATCHER)
    model = imported(text[: routine.span.char_start] + text[routine.span.char_end :])
    changed = execute(
        model,
        ManagerOperation(
            "rename",
            "pko",
            "update",
            target_id=model.pko[1].logical_id,
            patch=PkoPatch(name="Renamed"),
        ),
    )
    assert not any(u.name == DISPATCHER for u in changed.code_units)
    assert not changed.dispatcher_cases
    round_trip(changed)


@pytest.mark.parametrize("empty_first", [True, False])
def test_n2_dispatcher_overlap_retains_only_its_method(empty_first):
    source = base()
    baseline = imported(render(source).text)
    branch = branch_text(source, ITEM)
    if empty_first:
        changed_branch = (
            '\tЕсли ИмяПроцедуры = "Заглушка" Тогда\n'
            + branch.replace("\tЕсли", "\tИначеЕсли", 1)
            + "\t\tCompute(1);\n"
        )
    else:
        changed_branch = branch + '\t\tCompute(1);\n\tИначеЕсли ИмяПроцедуры = "Заглушка" Тогда\n'
    text = render(source).text.replace(branch, changed_branch)
    model, report = import_manager(read_manager_text(text), project_id="review-d2")
    dispatcher = next(u for u in model.code_units if u.name == DISPATCHER)
    assert dispatcher.state == "retained"
    assert any(e.logical_id == dispatcher.logical_id and e.reason for e in report.entries)
    assert any(
        b.kind == "routine" and f"Процедура {DISPATCHER}" in b.text for b in model.retained_blocks
    )
    assert all(
        u.state == next(v.state for v in baseline.code_units if v.name == u.name)
        for u in model.code_units
        if u.logical_id != dispatcher.logical_id
    )
    assert all(r.state == "editable" for r in model.pko)
    assert not any(b.kind == "layout_overlap" for b in model.retained_blocks)
    assert not checks(model)["ed.writer.dispatcher_literal_duplicate"]
    assert render(model).text == text
    changed = execute(
        model,
        ManagerOperation(
            "fresh", "algorithm", "create", patch=AlgorithmPatch(name="Fresh", body="X = 1;")
        ),
    )
    assert dispatcher.body == next(
        u.body for u in changed.code_units if u.logical_id == dispatcher.logical_id
    )
    round_trip(model)
    round_trip(changed)


@pytest.mark.parametrize(
    "extra,expected",
    [("Доп = 0", 0), ("Доп = 0, Ещё = Неопределено", 0), ("Доп", 1), ("Доп = 0, Ещё", 1)],
)
def test_n3_handler_signature_accepts_only_optional_extra_parameters(extra, expected):
    text = render(base()).text.replace(
        f"Процедура {OTHER}(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)",
        f"Процедура {OTHER}(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки, {extra})",
    )
    model = imported(text)
    assert checks(model)["ed.writer.handler_signature"] == expected
    assert checks(model)["ed.writer.dispatcher_arguments"] == expected


def ambiguous_handler_model():
    source = base()
    unit = next(u for u in source.code_units if u.name == OTHER)
    signature = f"Процедура {OTHER}(ДанныеИБ, ДанныеXDTO, КомпонентыОбмена, СтекВыгрузки)"
    frame = signature + unit.body + "КонецПроцедуры"
    doubled = (
        "#Если Сервер Тогда\n"
        + frame
        + "\n#Иначе\n"
        + frame.replace("Y = 2", "Y = 3")
        + "\n#КонецЕсли"
    )
    return imported(render(source).text.replace(frame, doubled))


def test_n4_deleting_rule_lists_unbound_ambiguous_methods_and_branch():
    model = ambiguous_handler_model()
    addresses = model_addresses(model)
    units = [u for u in model.code_units if u.name == OTHER]
    assert len(units) == 2 and all(u.state == "retained" for u in units)
    operation = ManagerOperation("delete", "pko", "delete", target_id=model.pko[1].logical_id)
    plan = planned(model, operation)
    assert not plan.failures
    notice = next((n for n in plan.notices if n.code == "orphan_handler"), None)
    assert notice is not None and "больше ни к чему не привязан" in notice.message
    expected = [addresses[u.logical_id] for u in units]
    expected += [addresses[c.logical_id] for c in model.dispatcher_cases if c.name == OTHER]
    assert len(notice.references) == 3
    assert all(any(ref.startswith(address) for ref in notice.references) for address in expected)
    with pytest.raises(ManagerOperationError) as error:
        apply(
            model,
            (operation,),
            expected_revision=model.revision,
            expected_preview_hash=plan.preview_hash,
            confirmations=tuple((n.code, n.notice_hash) for n in plan.notices if n != notice),
        )
    assert any(f.reason == "confirmation_required" for f in error.value.failures)
    changed = execute(model, operation)
    assert [u for u in changed.code_units if u.name == OTHER] == units
    assert branch_text(changed, OTHER) == branch_text(model, OTHER)
    round_trip(changed)


def test_n4_ambiguous_methods_with_another_binding_are_not_declared_orphaned():
    model = ambiguous_handler_model()
    other = model.pko[1]
    owner = model.pko[0]
    model = replace(
        model,
        pko=(
            replace(owner, events=(replace(owner.events[0], target=other.events[0].target),)),
            other,
        ),
    ).with_revision()
    plan = planned(model, ManagerOperation("delete", "pko", "delete", target_id=other.logical_id))
    assert not any(n.code == "orphan_handler" for n in plan.notices)
