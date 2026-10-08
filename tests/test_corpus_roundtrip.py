"""Импорт и round-trip всех макетов правил корпуса (задачи 2.2–2.4)."""

from functools import cache

import pytest
from lxml import etree

from kd_rules_mcp.kd2.canonical import canonical_diff, canonical_form, parse_xml
from kd_rules_mcp.kd2.rules_io import (
    dump_rules,
    load_exchange_rules,
    load_registration_rules,
    load_rules,
)
from tests.corpus import ALL_KINDS, EXCHANGE_KINDS, REGISTRATION_KINDS, CorpusFile, corpus_params

pytestmark = pytest.mark.corpus


@cache
def _raw(item: CorpusFile) -> bytes:
    return item.path.read_bytes()


def _codes(root: etree._Element, path: str) -> list[str]:
    """Коды правил прямо по XML, независимо от модели."""
    result: list[str] = []
    for element in root.iterfind(path):
        code = element.findtext("Код")
        result.append((code if code is not None else element.get("Имя", "")).strip())
    return result


@pytest.mark.parametrize("item", corpus_params(EXCHANGE_KINDS))
def test_exchange_rules_import(item: CorpusFile) -> None:
    raw = _raw(item)
    rules = load_exchange_rules(item.path)
    root = parse_xml(raw)
    assert [r.code.strip() for r in rules.pko()] == _codes(
        root, "ПравилаКонвертацииОбъектов//Правило"
    )
    assert [r.code.strip() for r in rules.pvd()] == _codes(root, "ПравилаВыгрузкиДанных//Правило")
    assert [r.code.strip() for r in rules.algorithms()] == _codes(root, "Алгоритмы//Алгоритм")
    assert [r.code.strip() for r in rules.queries()] == _codes(root, "Запросы//Запрос")
    assert all(not node.unknown for node in rules.root.walk_all())


@pytest.mark.parametrize("item", corpus_params(EXCHANGE_KINDS))
def test_exchange_rules_round_trip(item: CorpusFile) -> None:
    raw = _raw(item)
    out = dump_rules(load_exchange_rules(item.path))
    assert canonical_diff(canonical_form(raw), canonical_form(out)) == []


@pytest.mark.parametrize("item", corpus_params(REGISTRATION_KINDS))
def test_registration_rules_round_trip(item: CorpusFile) -> None:
    raw = _raw(item)
    rules = load_registration_rules(item.path)
    root = parse_xml(raw)
    assert len(rules.plan_content()) == len(root.findall("СоставПланаОбмена/Элемент"))
    assert [r.code.strip() for r in rules.rules()] == _codes(
        root, "ПравилаРегистрацииОбъектов//Правило"
    )
    assert all(not node.unknown for node in rules.root.walk_all())
    out = dump_rules(rules)
    assert canonical_diff(canonical_form(raw), canonical_form(out)) == []


@pytest.mark.parametrize("item", corpus_params(EXCHANGE_KINDS))
def test_exchange_rules_header(item: CorpusFile) -> None:
    """В заголовке правил заданы имена конфигураций источника и приёмника."""
    rules = load_exchange_rules(item.path)
    assert rules.source_name
    assert rules.target_name


@pytest.mark.parametrize("item", corpus_params(REGISTRATION_KINDS))
def test_registration_rules_name_their_exchange_plan(item: CorpusFile) -> None:
    """Правила регистрации из макета плана обмена ссылаются на этот же план."""
    rules = load_registration_rules(item.path)
    assert rules.exchange_plan == item.exchange_plan


def _mix_endings(raw: bytes) -> bytes:
    parts: list[bytes] = []
    for index, line in enumerate(raw.splitlines(keepends=True)):
        body = line.removesuffix(b"\r\n").removesuffix(b"\n").removesuffix(b"\r")
        ending = b""
        if line.endswith((b"\n", b"\r")):
            ending = b"\n" if index % 3 == 0 else b"\r\n"
        parts.append(body + ending)
    return b"".join(parts)


@pytest.mark.parametrize("item", corpus_params(ALL_KINDS))
def test_mixed_newlines_round_trip_is_byte_identical(item: CorpusFile) -> None:
    """Смешанные концы строк сохраняются, если макет и так воспроизводится побайтно.

    Круг идёт по файлу, который писатель уже отдаёт без изменений: иначе правка
    концов строк смешивается с нормализацией XML. Большой макет здесь не гоняем —
    побайтный круг таких файлов в закрытом тесте живых правил.
    """
    raw = _raw(item)
    if len(raw) > 300_000:
        pytest.skip("большой макет")
    if dump_rules(load_rules(raw)) != raw:
        pytest.skip("макет не воспроизводится побайтно")
    mixed = _mix_endings(raw)
    assert dump_rules(load_rules(mixed)) == mixed
