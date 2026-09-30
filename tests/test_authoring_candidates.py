"""Кандидаты сопоставления (спецификация `rules-authoring`, «Кандидаты сопоставления»)."""

from pathlib import Path

import pytest

from kd2_rules_mcp.authoring.candidates import (
    KD_NAME_ANALOGS,
    PRIMITIVE_TO_REFERENCE,
    Candidate,
    Confidence,
    object_candidates,
    property_candidates,
    value_candidates,
)
from kd2_rules_mcp.structures.queries import NotFound
from tests.sqlite_structure import StructureBuilder


def _pair(tmp_path: Path) -> tuple[StructureBuilder, StructureBuilder]:
    return StructureBuilder(tmp_path / "source.sqlite"), StructureBuilder(
        tmp_path / "target.sqlite"
    )


def _names(candidate: Candidate) -> tuple[str | None, str | None]:
    return (
        candidate.source.name if candidate.source else None,
        candidate.target.name if candidate.target else None,
    )


def _by_names(candidates: list[Candidate]) -> dict[tuple[str | None, str | None], Candidate]:
    return {_names(item): item for item in candidates}


def _properties(
    source: StructureBuilder, target: StructureBuilder, name: str = "Документ.Ведомость"
) -> list:
    result = property_candidates(source.conn, target.conn, name, name)
    assert not isinstance(result, NotFound)
    return result


def test_document_properties_exact_and_no_pair(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add(
        "Документ",
        "Ведомость",
        [
            ("Реквизит", "Организация", "", "СправочникСсылка.Организации", []),
            ("Реквизит", "Комментарий", "", "Строка", []),
            ("Реквизит", "ТолькоВИсточнике", "", "Число", []),
        ],
    )
    target.add(
        "Документ",
        "Ведомость",
        [
            ("Реквизит", "Организация", "", "СправочникСсылка.Организации", []),
            ("Реквизит", "Комментарий", "", "Строка", []),
            ("Реквизит", "ТолькоВПриемнике", "", "Число", []),
        ],
    )
    found = _by_names(_properties(source, target))
    assert found[("Организация", "Организация")].confidence is Confidence.EXACT
    assert found[("Организация", "Организация")].auto
    assert found[("Комментарий", "Комментарий")].confidence is Confidence.EXACT
    assert found[(None, "ТолькоВПриемнике")].confidence is Confidence.NO_PAIR
    assert found[("ТолькоВИсточнике", None)].confidence is Confidence.NO_PAIR


def test_same_name_other_kind_is_not_a_pair(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add("Документ", "Ведомость", [("Реквизит", "Сумма", "", "Число", [])])
    target.add("Документ", "Ведомость", [("Ресурс", "Сумма", "", "Число", [])])
    found = _by_names(_properties(source, target))
    assert set(found) == {("Сумма", None), (None, "Сумма")}


@pytest.mark.parametrize(("source_name", "target_name"), sorted(KD_NAME_ANALOGS.items()))
def test_kd_name_analogs(tmp_path: Path, source_name: str, target_name: str) -> None:
    source, target = _pair(tmp_path)
    kind_type = "Дата" if "Дата" in source_name else "Строка"
    source.add("Документ", "Ведомость", [("Свойство", source_name, "", kind_type, [])])
    target.add("Документ", "Ведомость", [("Свойство", target_name, "", kind_type, [])])
    [candidate] = _properties(source, target)
    assert _names(candidate) == (source_name, target_name)
    assert candidate.confidence is Confidence.KD_SYNONYM
    assert candidate.auto


def test_exact_name_wins_over_analog(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add(
        "Документ",
        "Ведомость",
        [("Свойство", "Номер", "", "Строка", []), ("Свойство", "НомерДок", "", "Строка", [])],
    )
    target.add("Документ", "Ведомость", [("Свойство", "Номер", "", "Строка", [])])
    found = _by_names(_properties(source, target))
    assert found[("Номер", "Номер")].confidence is Confidence.EXACT
    assert found[("НомерДок", None)].confidence is Confidence.NO_PAIR


def test_primitive_to_reference_is_not_automatic(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add(
        "Документ",
        "Ведомость",
        [
            ("Реквизит", "Контрагент", "", "Строка", []),
            ("Реквизит", "Ответственный", "", "СправочникСсылка.Пользователи", []),
            ("Реквизит", "Основание", "", "Строка\nЧисло", []),
        ],
    )
    target.add(
        "Документ",
        "Ведомость",
        [
            ("Реквизит", "Контрагент", "", "СправочникСсылка.Контрагенты", []),
            ("Реквизит", "Ответственный", "", "Строка", []),
            ("Реквизит", "Основание", "", "ДокументСсылка.Счет\nСтрока", []),
        ],
    )
    found = _by_names(_properties(source, target))
    banned = found[("Контрагент", "Контрагент")]
    assert banned.confidence is Confidence.EXACT
    assert not banned.auto
    assert banned.note == PRIMITIVE_TO_REFERENCE
    # Ссылка → примитив запретом не закрыта (КД, 517–564).
    assert found[("Ответственный", "Ответственный")].auto
    # Составной примитив в приёмник, допускающий ссылку, — тоже запрет.
    assert not found[("Основание", "Основание")].auto


def test_by_synonym_is_hint_only(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add(
        "Документ",
        "Ведомость",
        [
            ("Реквизит", "Касса", "Касса выплаты", "Строка", []),
            ("Реквизит", "А1", "Двойной", "Строка", []),
            ("Реквизит", "А2", "Двойной", "Строка", []),
        ],
    )
    target.add(
        "Документ",
        "Ведомость",
        [
            ("Реквизит", "КассаВыплаты", "касса выплаты", "Строка", []),
            ("Реквизит", "Б1", "Двойной", "Строка", []),
        ],
    )
    found = _by_names(_properties(source, target))
    hint = found[("Касса", "КассаВыплаты")]
    assert hint.confidence is Confidence.BY_SYNONYM
    assert not hint.auto
    # Синоним неоднозначен — подсказки нет.
    assert found[(None, "Б1")].confidence is Confidence.NO_PAIR


def test_table_section_children_match_inside_parent(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add(
        "Документ",
        "Ведомость",
        [
            ("ТабличнаяЧасть", "Зарплата", "", "", [("Реквизит", "Сумма", "", "Число", [])]),
            ("ТабличнаяЧасть", "Прочее", "", "", [("Реквизит", "Сотрудник", "", "Строка", [])]),
        ],
    )
    target.add(
        "Документ",
        "Ведомость",
        [
            (
                "ТабличнаяЧасть",
                "Зарплата",
                "",
                "",
                [
                    ("Реквизит", "Сумма", "", "Число", []),
                    ("Реквизит", "Сотрудник", "", "Строка", []),
                ],
            ),
        ],
    )
    found = _by_names(_properties(source, target))
    table = found[("Зарплата", "Зарплата")]
    children = _by_names(list(table.children))
    assert children[("Сумма", "Сумма")].confidence is Confidence.EXACT
    # «Сотрудник» есть только в другой табличной части источника.
    assert children[(None, "Сотрудник")].confidence is Confidence.NO_PAIR
    assert (
        table.children[0].target is not None and table.children[0].target.path == "Зарплата.Сумма"
    )


def test_objects_by_name_and_kind(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add("Справочник", "Организации")
    source.add("Справочник", "Ведомость")
    source.add("Справочник", "Кассы", synonym="Кассы организации")
    target.add("Справочник", "Организации")
    target.add("Документ", "Ведомость")
    target.add("Справочник", "КассыОрганизаций", synonym="Кассы организации")
    found = _by_names(object_candidates(source.conn, target.conn))
    assert found[("Организации", "Организации")].confidence is Confidence.EXACT
    assert found[("Ведомость", None)].confidence is Confidence.NO_PAIR
    assert found[(None, "Ведомость")].confidence is Confidence.NO_PAIR
    assert found[("Кассы", "КассыОрганизаций")].confidence is Confidence.BY_SYNONYM
    only_documents = object_candidates(source.conn, target.conn, kind="Документ")
    assert [_names(item) for item in only_documents] == [(None, "Ведомость")]


def test_values_by_name(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add("Перечисление", "Виды", values=[("Первый", ""), ("Второй", ""), ("Старый", "")])
    target.add("Перечисление", "Виды", values=[("Первый", ""), ("второй", ""), ("Новый", "")])
    result = value_candidates(source.conn, target.conn, "Перечисление.Виды", "Перечисление.Виды")
    assert not isinstance(result, NotFound)
    found = _by_names(result)
    assert found[("Первый", "Первый")].confidence is Confidence.EXACT
    assert found[("Второй", "второй")].confidence is Confidence.EXACT
    assert found[("Старый", None)].confidence is Confidence.NO_PAIR
    assert found[(None, "Новый")].confidence is Confidence.NO_PAIR


def test_missing_object_is_not_found(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    source.add("Документ", "Ведомость")
    target.add("Документ", "Ведомость")
    result = property_candidates(source.conn, target.conn, "Документ.Ведомость", "Документ.Нет")
    assert isinstance(result, NotFound)
    assert "Документ.Нет" in result.message
