"""Отрисовка обработчиков: шаблоны §5, слой, манифест и пилот."""

import hashlib
import importlib
import json
from collections import Counter
from pathlib import Path

import pytest

from kd_rules_mcp.authoring.ed.handler_render import (
    binding_record,
    procedure_records,
    render_handler_module,
)
from kd_rules_mcp.authoring.ed.handlers import (
    EVENT_PARAMETERS,
    HandlerBindingPlan,
    handler_name,
    operation_from_input,
)
from kd_rules_mcp.authoring.ed.manifest import GENERATOR_VERSION_V2
from kd_rules_mcp.authoring.ed.model import (
    AddAlgorithmicHeaderProperty,
    AddHeaderProperty,
    AuthoringTarget,
    Direction,
    ExtensionIdentity,
    HandlerEvent,
    PreserveMissingHeaderProperty,
    SetObjectHandler,
)
from kd_rules_mcp.ed import read_manager
from kd_rules_mcp.ed.layer_model import LayerDescriptor
from kd_rules_mcp.ed.layer_reader import read_extension_text
from kd_rules_mcp.ed.layers import compose_manager
from kd_rules_mcp.ed.model import Classification, ObjectRule
from kd_rules_mcp.validation.ed_layers import validate_effective_links, validate_layers
from kd_rules_mcp.validation.ed_projection import effective_document, select_context
from tests.test_ed_authoring_handlers import handler_inputs, reference_inputs
from tests.test_ed_layer_flow import HELPERS

DATA = Path(__file__).parent / "data/ed/authoring/handlers"
GOLDEN = DATA / "golden"
PREFIX = "доп_"
PILOT = Path(__file__).parents[1] / "docs/plans/evals/2026-10-04-ed-handlers-pilot"

# Отличия порождения от модулей пилота после замены имён. Текст генератора следует §5.
PILOT_DIFFERENCES = (
    "Комментарий-шапка: одна строка §5.1, в пилоте две строки протокола.",
    "Имя процедуры: <префикс>ПКО_<16 hex> вместо фиксированного имени пилота.",
    "Отправка Должностей (ЗУП): в пилоте §8.2 нет пустой строки после заголовка заполнителя "
    "и перед его КонецПроцедуры; генератор ставит их по §5.1.",
    "Пользователи (ЗУП, цепочка): вызов прежнего обработчика — первый оператор процедуры (§5.5); "
    "в пилоте перед ним есть присваивание «Было».",
)


def _canonical_operations(fixture: dict) -> tuple:
    rows = json.loads(fixture["canonical_utf8"])
    operations = []
    for row in rows:
        payload = {
            key: value for key, value in row.items() if key not in ("operation_id", "dependencies")
        }
        operation = operation_from_input(payload)
        assert operation.operation_id == row["operation_id"]
        operations.append(operation)
    return tuple(operations)


def _bindings(fixture: dict) -> tuple[HandlerBindingPlan, ...]:
    result = []
    for row in fixture["bindings"]:
        data = dict(row)
        data["target"] = AuthoringTarget(**data["target"])
        for key in ("parameters", "operation_ids", "previous_arguments"):
            data[key] = tuple(data[key])
        result.append(HandlerBindingPlan(**data))
    return tuple(result)


def _pko_names(operations, bindings) -> dict[str, str]:
    names = {}
    for item in (*operations, *bindings):
        address = item.target.pko_address
        names[address] = address.removeprefix("ПКО/")
    return names


def _render(fixture: dict, interface: int) -> str:
    operations = _canonical_operations(fixture)
    bindings = _bindings(fixture)
    return render_handler_module(
        operations,
        bindings,
        prefix=PREFIX,
        interface=interface,
        dispatcher_name=fixture["dispatcher_name"],
        dispatcher_order=tuple(fixture["dispatcher_order"]),
        pko_names=_pko_names(operations, bindings),
    )


def _scenarios():
    return sorted(
        path
        for path in (DATA / "dto").glob("*.json")
        if "expected_failure" not in path.read_text("utf-8")
    )


@pytest.mark.parametrize("path", _scenarios(), ids=lambda path: path.stem)
@pytest.mark.parametrize("interface", [1, 2, 3])
def test_dto_module_matches_golden_and_interface_pair(path, interface):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if interface not in fixture["interfaces"]:
        pytest.skip("Сценарий не задаёт этот интерфейс")
    text = _render(fixture, interface)
    assert "\r" not in text and text.endswith("\n") and not text.endswith("\n\n")
    assert text.count("&После") == 1 and text.count("&Вместо") == 1
    assert text.count("ПродолжитьВызов(ИмяПроцедуры, Параметры);") == 1
    if interface == 3:
        assert "Если Не ТолькоЗаголовки Тогда" in text
        golden = GOLDEN / path.stem / "module-v3.bsl"
    else:
        assert "ТолькоЗаголовки" not in text
        golden = GOLDEN / path.stem / "module.bsl"
    assert text.encode("utf-8") == golden.read_bytes()
    if interface == 1:
        assert text == _render(fixture, 2)


@pytest.mark.parametrize("path", _scenarios(), ids=lambda path: path.stem)
def test_dto_manifest_section_matches_golden(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    operations = _canonical_operations(fixture)
    bindings = _bindings(fixture)
    section = {
        "bindings": [binding_record(binding) for binding in bindings],
        "dispatcher_name": fixture["dispatcher_name"],
        "dispatcher_order": fixture["dispatcher_order"],
        "generator_version": GENERATOR_VERSION_V2,
        "procedures": [
            dict(item)
            for item in procedure_records(
                operations,
                bindings,
                dispatcher_name=fixture["dispatcher_name"],
                dispatcher_order=tuple(fixture["dispatcher_order"]),
                runtime_verified=fixture["plan_runtime_verified"],
            )
        ],
        "schema_version": 2,
    }
    assert section == json.loads((GOLDEN / path.stem / "manifest-section.json").read_text("utf-8"))


def test_refused_algorithmic_receive_has_no_module():
    fixture = json.loads(
        (DATA / "dto/algorithmic-receive-refused.json").read_text(encoding="utf-8")
    )
    assert fixture["expected_failure"] == "ed.author.not_supported"
    assert not (GOLDEN / "algorithmic-receive-refused").exists()


def _document(fixture: dict, interface: int):
    previous = next((row for row in fixture["bindings"] if row["previous_name"]), None)
    if fixture.get("context") == "reference" and interface == 2:
        return reference_inputs().document
    if previous:
        return handler_inputs(
            interface, event=previous["event"], previous=previous["previous_name"]
        ).document
    return handler_inputs(interface).document


def _prop_key(prop):
    return (
        prop.configuration_property,
        prop.format_property,
        prop.algorithm_flag,
        prop.conversion_rule,
    )


def _events(rule: ObjectRule) -> dict[str, str]:
    return {binding.event: binding.target_name for binding in rule.events}


@pytest.mark.parametrize("path", _scenarios(), ids=lambda path: path.stem)
@pytest.mark.parametrize("interface", [1, 2, 3])
def test_rendered_extension_reads_completely_and_changes_only_declared_operations(path, interface):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if interface not in fixture["interfaces"]:
        pytest.skip("Сценарий не задаёт этот интерфейс")
    operations = _canonical_operations(fixture)
    bindings = _bindings(fixture)
    document = _document(fixture, interface)
    text = _render(fixture, interface)
    layer = LayerDescriptor("L01-handlers", 1, "Обработчики", "<memory>", None, "")
    reading = read_extension_text(
        text,
        layer=layer,
        version=interface,
        helpers=HELPERS,
        targets={routine.name.casefold(): routine for routine in document.routines},
        path="handlers.bsl",
        file_id="handlers",
    )
    layered = compose_manager(document, readings=[reading], version=interface)
    baseline = compose_manager(document, version=interface)
    assert not reading.skips and not layered.skipped
    assert reading.coverage.line_classes.count(Classification.UNKNOWN) == 0
    assert layered.status == "complete"
    names = _pko_names(operations, bindings)
    for direction in ("send", "receive"):
        context = select_context(layered, direction)
        base_context = select_context(baseline, direction)
        assert all(entity.certainty == "known" for entity in context.entities)
        current = {rule.name: rule for rule in effective_document(layered, context).pko}
        base_rules = {rule.name: rule for rule in effective_document(baseline, base_context).pko}
        for name, rule in current.items():
            base_rule = base_rules[name]
            expected_props = []
            for op in operations:
                label = names[op.target.pko_address]
                if op.target.direction != direction or label != name:
                    continue
                if isinstance(op, AddHeaderProperty):
                    expected_props.append((op.configuration_attribute, op.format_property, 0, ""))
                elif isinstance(op, AddAlgorithmicHeaderProperty):
                    expected_props.append(
                        (op.configuration_attribute, op.format_property, 1, op.conversion_rule)
                    )
            assert Counter(_prop_key(prop) for prop in rule.properties) - Counter(
                _prop_key(prop) for prop in base_rule.properties
            ) == Counter(expected_props)
            assert not (
                Counter(_prop_key(prop) for prop in base_rule.properties)
                - Counter(_prop_key(prop) for prop in rule.properties)
            )
            expected_events = {
                binding.event: binding.handler_name
                for binding in bindings
                if binding.target.direction == direction
                and names[binding.target.pko_address] == name
            }
            after = _events(rule)
            before = _events(base_rule)
            for event, handler in expected_events.items():
                assert after[event] == handler
            for event, handler in before.items():
                if event not in expected_events:
                    assert after.get(event) == handler
            assert set(after) <= set(before) | set(expected_events)
        chains = {chain.target_name: chain for chain in context.dispatch_chains}
        for binding in bindings:
            if binding.target.direction == direction:
                assert chains[binding.handler_name].resolution == "call"
        link_delta = Counter(
            (issue.check, issue.level, issue.address, issue.message)
            for issue in validate_effective_links(layered, context).issues
        ) - Counter(
            (issue.check, issue.level, issue.address, issue.message)
            for issue in validate_effective_links(baseline, base_context).issues
        )
        assert not link_delta
    for call in layered.previous_calls:
        assert call.call_count == 1
        assert call.target_name in {binding.previous_name for binding in bindings}
    layer_delta = Counter(
        (issue.check, issue.level, issue.address, issue.message)
        for issue in validate_layers(layered).issues
    ) - Counter(
        (issue.check, issue.level, issue.address, issue.message)
        for issue in validate_layers(baseline).issues
    )
    assert not layer_delta
    assert not any(
        issue.check == "ed.layer.handler.touches_rules" for issue in validate_layers(layered).issues
    )
    if interface == 3:
        for direction in ("send", "receive"):
            assert (
                effective_document(layered, select_context(layered, direction, True)).pko
                == effective_document(baseline, select_context(baseline, direction, True)).pko
            )


def _spec_spacing(text: str) -> str:
    """Пустые строки §5.1 вокруг тела заполнителя. Повтор не добавляет вторую."""
    lines = text.splitlines()
    try:
        header = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("Процедура ") and "ЗаполнитьПравилаКонвертацииОбъектов" in line
        )
        end = next(index for index, line in enumerate(lines) if line == "КонецПроцедуры")
    except StopIteration:
        return text if text.endswith("\n") else text + "\n"
    if lines[header + 1] != "":
        lines.insert(header + 1, "")
        end += 1
    if lines[end - 1] != "":
        lines.insert(end, "")
    return "\n".join(lines) + "\n"


def _move_previous_call_first(text: str, previous: str) -> str:
    """Пилот H8 считает маркер до прежнего вызова; §5.5 ставит вызов первым оператором."""
    lines = text.splitlines()
    call = next(index for index, line in enumerate(lines) if previous in line and "(" in line)
    header = max(index for index in range(call) if lines[index].startswith("Процедура "))
    body_start = header + 1
    if lines[body_start] == "":
        body_start += 1
    statement = lines.pop(call)
    lines.insert(body_start, statement)
    return "\n".join(lines) + "\n"


def _pilot_module(side: str) -> str:
    return next((PILOT / side / "extension/CommonModules").glob("*/Ext/Module.bsl")).read_text(
        "utf-8"
    )


def _without_comments(text: str) -> str:
    return "".join(line + "\n" for line in text.splitlines() if not line.startswith("//"))


def _direction(value: str) -> Direction:
    if value == "send":
        return "send"
    if value == "receive":
        return "receive"
    raise AssertionError(value)


def _event(value: str) -> HandlerEvent:
    if value == "ПриОтправкеДанных":
        return "ПриОтправкеДанных"
    if value == "ПриКонвертацииДанныхXDTO":
        return "ПриКонвертацииДанныхXDTO"
    if value == "ПередЗаписьюПолученныхДанных":
        return "ПередЗаписьюПолученныхДанных"
    raise AssertionError(value)


@pytest.mark.slow
def test_closed_corpus_pilot_modules_match_after_listed_differences():
    """Порождение для объектов пилота. Отличия — только перечень PILOT_DIFFERENCES."""
    try:
        private = importlib.import_module("tests.private.corpus_private")
    except ImportError:
        pytest.skip("Закрытый корпус отсутствует")
    from tests.session_inputs import corpus_structure

    prefix = "кд3а_"
    identity = ExtensionIdentity("кд3а_Авторинг", prefix)
    cases = (
        {
            "side": "bp",
            "pko": "Справочник_Должности",
            "direction": "receive",
            "event": "ПередЗаписьюПолученныхДанных",
            "attribute": "кд3а_Заметка",
            "format_property": "Комментарий",
            "pilot_handler": "кд3а_Сохранить",
            "mode": "preserve",
        },
        {
            "side": "zup",
            "pko": "Справочник_Должности_Отправка",
            "direction": "send",
            "event": "ПриОтправкеДанных",
            "attribute": "кд3а_Заметка",
            "format_property": "Комментарий",
            "pilot_handler": "кд3а_Отправить",
            "mode": "send",
        },
        {
            "side": "zup-h8",
            "pko": "Справочник_Пользователи",
            "direction": "send",
            "event": "ПриОтправкеДанных",
            "attribute": "кд3а_Заметка",
            "format_property": "Комментарий",
            "pilot_handler": "кд3а_ОтправитьПользователя",
            "mode": "chain",
            "extra_pko": "Справочник_Должности_Отправка",
        },
    )
    compared = 0
    for case in cases:
        modules = list((PILOT / case["side"] / "extension/CommonModules").glob("*/Ext/Module.bsl"))
        assert len(modules) == 1
        manager = modules[0].parents[1].name
        root = next(
            (
                private.project_dir(project)
                for project in private.PROJECTS
                if (
                    private.project_dir(project) / "CommonModules" / manager / "Ext/Module.bsl"
                ).is_file()
            ),
            None,
        )
        if root is None:
            pytest.skip("Выгрузки менеджеров пилота недоступны")
        document_path = root / "CommonModules" / manager / "Ext/Module.bsl"
        before = hashlib.sha256(document_path.read_bytes()).hexdigest()
        previous = ""
        with corpus_structure(root):
            document = read_manager(document_path)
        rule = next(item for item in document.pko if item.declared_name == case["pko"])
        direction = _direction(case["direction"])
        event = _event(case["event"])
        target = AuthoringTarget(
            "Корпус", "Main", "Пилот", None, "1.20", direction, "ПКО/" + case["pko"]
        )
        operations: list = []
        bindings = []
        if case["mode"] == "preserve":
            prop = AddHeaderProperty(target, case["attribute"], case["format_property"])
            preset = PreserveMissingHeaderProperty(target, prop.operation_id)
            operations.extend((prop, preset))
            bindings.append(
                HandlerBindingPlan(
                    target,
                    event,
                    handler_name(identity.prefix, case["pko"], direction, event),
                    EVENT_PARAMETERS[event],
                    (preset.operation_id,),
                    runtime_verified=True,
                    manager_interface=document.manager_version or 2,
                )
            )
        elif case["mode"] == "send":
            body = '\tДанныеXDTO.Вставить("Комментарий", "ВЫЧИСЛЕНО:" + ДанныеИБ.кд3а_Заметка);\n'
            handler = SetObjectHandler(target, event, body, "", "none")
            prop = AddAlgorithmicHeaderProperty(
                target, case["attribute"], case["format_property"], handler.operation_id, ""
            )
            operations.extend((handler, prop))
            bindings.append(
                HandlerBindingPlan(
                    target,
                    event,
                    handler_name(identity.prefix, case["pko"], direction, event),
                    EVENT_PARAMETERS[event],
                    (handler.operation_id,),
                    runtime_verified=True,
                    manager_interface=document.manager_version or 2,
                )
            )
        else:
            extra = AuthoringTarget(
                "Корпус",
                "Main",
                "Пилот",
                None,
                "1.20",
                "send",
                "ПКО/" + case["extra_pko"],
            )
            operations.append(AddHeaderProperty(extra, case["attribute"], case["format_property"]))
            previous = next(
                (binding.target_name for binding in rule.events if binding.event == event),
                "",
            )
            assert previous
            pilot_text = _pilot_module(case["side"])
            start = pilot_text.index(f"Процедура {case['pilot_handler']}(")
            end = pilot_text.index("\nКонецПроцедуры", start)
            body = "\n".join(
                line for line in pilot_text[start:end].splitlines()[1:] if previous not in line
            )
            body = body.strip("\n") + "\n"
            handler = SetObjectHandler(target, event, body, previous, "after_existing")
            operations.append(handler)
            bindings.append(
                HandlerBindingPlan(
                    target,
                    event,
                    handler_name(identity.prefix, case["pko"], direction, event),
                    EVENT_PARAMETERS[event],
                    (handler.operation_id,),
                    previous,
                    EVENT_PARAMETERS[event],
                    runtime_verified=True,
                    manager_interface=document.manager_version or 2,
                )
            )
        names = [binding.handler_name for binding in bindings]
        text = render_handler_module(
            tuple(operations),
            tuple(bindings),
            prefix=prefix,
            interface=2,
            dispatcher_name=prefix + "Диспетчер",
            dispatcher_order=tuple(sorted(names)),
            pko_names={
                item.target.pko_address: item.target.pko_address.removeprefix("ПКО/")
                for item in operations
            },
        )
        pilot = _pilot_module(case["side"])
        mapping = {names[0]: "HANDLER", case["pilot_handler"]: "HANDLER"}
        generated = _without_comments(text)
        expected = _spec_spacing(_without_comments(pilot))
        for source, dest in mapping.items():
            generated = generated.replace(source, dest)
            expected = expected.replace(source, dest)
        if case["mode"] == "chain":
            expected = _move_previous_call_first(expected, previous)
        assert generated == expected, "\n".join(PILOT_DIFFERENCES)
        assert hashlib.sha256(document_path.read_bytes()).hexdigest() == before
        compared += 1
    assert compared == 3


def _dto_plan(path: Path):
    from kd_rules_mcp.authoring.ed.canonical import canonicalize_operations
    from kd_rules_mcp.authoring.ed.handlers import merge_operations
    from kd_rules_mcp.validation.ed_authoring import prepare_handler_operations
    from tests.test_ed_authoring_model import IDENTITY

    fixture = json.loads(path.read_text(encoding="utf-8"))
    operations = tuple(operation_from_input(op) for op in fixture["input"])
    previous = next(
        (op for op in operations if isinstance(op, SetObjectHandler) and op.expected_previous),
        None,
    )
    value = (
        reference_inputs()
        if fixture.get("context") == "reference"
        else handler_inputs(
            event=previous.event if previous else None,
            previous=previous.expected_previous if previous else "",
        )
    )
    canonical = canonicalize_operations(value, operations)
    if "previous_input" in fixture:
        earlier = tuple(operation_from_input(op) for op in fixture["previous_input"])
        canonical = merge_operations(
            earlier, canonical, drop_operations=tuple(fixture["drop_operations"])
        )
    plan = prepare_handler_operations(value, canonical, IDENTITY, version_scope="manager")
    return value, plan


@pytest.mark.parametrize("path", _scenarios(), ids=lambda path: path.stem)
def test_dto_kit_roundtrips_from_disk(path):
    """Полный комплект с нуля: золотой модуль, manifest и повтор с диска без изменений."""
    from kd_rules_mcp.authoring.ed.artifacts import previous_artifact
    from kd_rules_mcp.authoring.ed.render import render_handlers_authoring
    from tests.test_ed_authoring_model import IDENTITY
    from tests.test_ed_authoring_render import descriptions

    value, plan = _dto_plan(path)
    bundle = render_handlers_authoring(value, plan, IDENTITY, descriptions())
    module = next(
        item for item in bundle.files if item.startswith("modules/") and item.endswith(".bsl")
    )
    assert bundle.files[module] == (GOLDEN / path.stem / "module.bsl").read_bytes()
    extension = next(
        item for item in bundle.files if item.startswith("extension/") and item.endswith(".bsl")
    )
    assert bundle.files[extension] == bundle.files[module]
    assert previous_artifact(bundle.files) == bundle.manifest
    again = render_handlers_authoring(
        value,
        plan,
        IDENTITY,
        descriptions(),
        previous_manifest=bundle.manifest,
        previous_files=bundle.files,
    )
    assert again.status == "unchanged"
    assert again.files == bundle.files
    assert render_handlers_authoring(value, plan, IDENTITY, descriptions()).files == bundle.files


def test_procedure_fingerprint_covers_a_body_that_mentions_the_terminator():
    """Комментарий и строка «КонецПроцедуры» не обрезают отпечаток; правка хвоста видна."""
    from dataclasses import replace

    from kd_rules_mcp.authoring.ed.artifacts import previous_artifact
    from kd_rules_mcp.authoring.ed.handler_render import procedure_block
    from kd_rules_mcp.authoring.ed.manifest import sha256
    from kd_rules_mcp.authoring.ed.model import AuthoringPreconditionError
    from kd_rules_mcp.authoring.ed.render import render_handlers_authoring
    from kd_rules_mcp.validation.ed_authoring import prepare_handler_operations
    from tests.test_ed_authoring_handlers import handler
    from tests.test_ed_authoring_model import IDENTITY
    from tests.test_ed_authoring_render import descriptions

    body = '// КонецПроцедуры\nДанныеXDTO.Вставить("Комментарий", "КонецПроцедуры");\n'
    value = handler_inputs()
    plan = prepare_handler_operations(
        value, (handler(body=body),), IDENTITY, version_scope="manager"
    )
    bundle = render_handlers_authoring(value, plan, IDENTITY, descriptions())
    module = next(
        item for item in bundle.files if item.startswith("modules/") and item.endswith(".bsl")
    )
    text = bundle.files[module].decode("utf-8")
    name = plan.bindings[0].handler_name
    block = procedure_block(text, name)
    assert "// КонецПроцедуры\n" in block
    assert '"КонецПроцедуры"' in block
    assert block.endswith("КонецПроцедуры\n")
    assert 'Вставить("Комментарий", "КонецПроцедуры")' in block
    edited = text.replace(
        'ДанныеXDTO.Вставить("Комментарий", "КонецПроцедуры");\n',
        'ДанныеXDTO.Вставить("Комментарий", "КонецПроцедуры"); // правка\n',
        1,
    )
    edited_block = procedure_block(edited, name)
    assert edited_block != block
    assert sha256(edited_block.encode("utf-8")) != sha256(block.encode("utf-8"))
    files = dict(bundle.files)
    payload = edited.encode("utf-8")
    for path in files:
        if path.endswith("Module.bsl"):
            files[path] = payload
    manifest = replace(
        bundle.manifest,
        file_hashes={
            **bundle.manifest.file_hashes,
            **{path: sha256(files[path]) for path in files if path.endswith("Module.bsl")},
        },
    )
    files["manifest.json"] = manifest.to_bytes()
    with pytest.raises(AuthoringPreconditionError) as error:
        previous_artifact(files)
    assert error.value.failures[0].id == "ed.author.owned_content_changed"
