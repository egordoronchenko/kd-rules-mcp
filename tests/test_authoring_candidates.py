"""Кандидаты сопоставления (спецификация `rules-authoring`, «Кандидаты сопоставления»)."""

from pathlib import Path

import pytest

from kd_rules_mcp.authoring.candidates import (
    BY_COMPOSITION_HINT,
    KD_NAME_ANALOGS,
    PRIMITIVE_TO_REFERENCE,
    Candidate,
    Confidence,
    object_candidates,
    object_candidates_report,
    property_candidates,
    value_candidates,
)
from kd_rules_mcp.structures.queries import NotFound
from tests.sqlite_structure import Prop, StructureBuilder


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


def _composition_props(*names: str, kind: str = "Реквизит") -> list[Prop]:
    return [(kind, name, "", "Строка", []) for name in names]


def test_composition_renamed_catalog(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    names = ("ИНН", "КПП", "ОГРН", "Адрес", "Телефон", "ВидКонтрагента")
    source.add("Справочник", "Контрагенты", _composition_props(*names), synonym="Контрагенты")
    target.add("Справочник", "До")
    target.add("Справочник", "Партнеры", _composition_props(*names, "Сайт"), synonym="Партнеры")
    target.add("Справочник", "После")
    found = object_candidates(source.conn, target.conn)
    assert [_names(item) for item in found] == [
        (None, "До"),
        ("Контрагенты", "Партнеры"),
        (None, "После"),
    ]
    pair = found[1]
    assert pair.confidence is Confidence.BY_COMPOSITION
    assert pair.auto is False
    assert pair.share == pytest.approx(6 / 7)
    assert pair.common == 6
    assert pair.note == BY_COMPOSITION_HINT
    disabled = object_candidates_report(source.conn, target.conn, composition=False)
    assert disabled.composition_pairs == disabled.composition_ambiguous == 0
    assert all(item.confidence is Confidence.NO_PAIR for item in disabled.candidates)


@pytest.mark.parametrize("uppercase", [False, True])
@pytest.mark.parametrize("kind", ["Перечисление", "ТочкаМаршрутаБизнесПроцесса"])
def test_composition_renamed_values(tmp_path: Path, uppercase: bool, kind: str) -> None:
    source, target = _pair(tmp_path)
    names = [f"Статус{index}" for index in range(8)]
    source.add(kind, "СтатусыЭД", values=[(name, "") for name in names])
    target.add(
        kind,
        "СтатусыЭДО",
        values=[(f" {name.upper()} " if uppercase else name, "") for name in names],
    )
    [pair] = object_candidates(source.conn, target.conn)
    assert _names(pair) == ("СтатусыЭД", "СтатусыЭДО")
    assert pair.confidence is Confidence.BY_COMPOSITION
    assert pair.share == 1.0 and pair.common == 8
    assert pair.auto is False


@pytest.mark.parametrize(("count", "expected"), [(20, 0), (10, 1)])
def test_composition_noise_object_count(tmp_path: Path, count: int, expected: int) -> None:
    source, target = _pair(tmp_path)
    noise = ("Комментарий", "Организация", "Ответственный", "Сумма", "Валюта")
    for builder, renamed, own in ((source, "А", "Источник"), (target, "Б", "Приемник")):
        builder.add("Документ", renamed, _composition_props(*noise, own))
        # Пары по имени тоже входят в знаменатель и частоты шума.
        for index in range(count - 1):
            builder.add("Документ", f"Типовой{index}", _composition_props(*noise))
    report = object_candidates_report(source.conn, target.conn, "Документ")
    assert report.composition_pairs == expected
    assert report.composition_ambiguous == 0
    found = _by_names(report.candidates)
    if expected:
        assert found[("А", "Б")].share == pytest.approx(5 / 7)
    else:
        assert found[("А", None)].confidence is Confidence.NO_PAIR
        assert found[(None, "Б")].confidence is Confidence.NO_PAIR


@pytest.mark.parametrize("noisy_side", ["source", "target"])
def test_composition_noise_union_and_frequency_boundary(tmp_path: Path, noisy_side: str) -> None:
    source, target = _pair(tmp_path)
    shared = ("А", "Б", "В", "Г", "Д")
    source.add("Документ", "Источник", _composition_props(*shared, "СвоёА"))
    target.add("Документ", "Приемник", _composition_props(*shared, "СвоёБ"))
    noisy = source if noisy_side == "source" else target
    for index in range(19):
        # Ровно 3 из 20 (15 %) содержат общие имена.
        noisy.add("Документ", f"Шум{index}", _composition_props(*(shared if index < 2 else ())))
    report = object_candidates_report(source.conn, target.conn)
    assert report.composition_pairs == report.composition_ambiguous == 0
    assert all(item.confidence is Confidence.NO_PAIR for item in report.candidates)


@pytest.mark.parametrize(
    ("common", "extra", "expected"), [(3, 0, 0), (5, 3, 0), (5, 2, 1), (7, 3, 1)]
)
def test_composition_few_names_and_share_threshold(
    tmp_path: Path, common: int, extra: int, expected: int
) -> None:
    source, target = _pair(tmp_path)
    values = [(f"Общее{index}", "") for index in range(common)]
    source.add("Перечисление", "А", values=values)
    target.add("Перечисление", "Б", values=values + [(f"Своё{i}", "") for i in range(extra)])
    report = object_candidates_report(source.conn, target.conn)
    assert report.composition_pairs == expected
    assert report.composition_ambiguous == 0
    if expected:
        [pair] = report.candidates
        assert pair.common == common
        assert pair.share == pytest.approx(common / (common + extra))
    else:
        assert set(_by_names(report.candidates)) == {("А", None), (None, "Б")}


@pytest.mark.parametrize("two_sources", [False, True])
def test_composition_ambiguous_both_directions(tmp_path: Path, two_sources: bool) -> None:
    source, target = _pair(tmp_path)
    props = _composition_props("А", "Б", "В", "Г", "Д")
    source.add("Справочник", "Источник", props)
    target.add("Справочник", "Приемник", props)
    (source if two_sources else target).add("Справочник", "Второй", props)
    report = object_candidates_report(source.conn, target.conn)
    assert report.composition_pairs == 0
    assert report.composition_ambiguous == 2
    assert len(report.candidates) == 3
    assert all(item.confidence is Confidence.NO_PAIR for item in report.candidates)


@pytest.mark.parametrize("by_synonym", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_composition_exact_or_synonym_pair_wins(
    tmp_path: Path, by_synonym: bool, reverse: bool
) -> None:
    source, target = _pair(tmp_path)
    props = _composition_props("А", "Б", "В", "Г", "Д")
    source.add("Справочник", "А", props, synonym="Синоним")
    target_name = "ДругоеИмя" if by_synonym else "А"
    target.add("Справочник", target_name, props, synonym="Синоним")
    target.add("Справочник", "Б", props)
    if reverse:
        source, target = target, source
    report = object_candidates_report(source.conn, target.conn)
    assert report.composition_pairs == report.composition_ambiguous == 0
    found = _by_names(report.candidates)
    expected_pair = (target_name, "А") if reverse else ("А", target_name)
    assert found[expected_pair].confidence is (
        Confidence.BY_SYNONYM if by_synonym else Confidence.EXACT
    )
    assert found[("Б", None) if reverse else (None, "Б")].confidence is Confidence.NO_PAIR


def test_composition_different_object_kinds(tmp_path: Path) -> None:
    source, target = _pair(tmp_path)
    props = _composition_props("А", "Б", "В", "Г", "Д")
    source.add("Справочник", "Источник", props)
    target.add("Документ", "Приемник", props)
    found = object_candidates(source.conn, target.conn)
    assert len(found) == 2
    assert all(item.confidence is Confidence.NO_PAIR for item in found)


@pytest.mark.parametrize("prop_kind", ["Реквизит", "Измерение", "Ресурс"])
def test_composition_root_properties_casefold(tmp_path: Path, prop_kind: str) -> None:
    source, target = _pair(tmp_path)
    names = ("Альфа", "Бета", "Гамма", "Дельта", "Эпсилон")
    source.add("РегистрСведений", "Источник", _composition_props(*names, kind=prop_kind))
    target.add(
        "регистрсведений",
        "Приемник",
        _composition_props(*(f" {name.upper()} " for name in names), kind=prop_kind),
    )
    [pair] = object_candidates(source.conn, target.conn)
    assert pair.confidence is Confidence.BY_COMPOSITION
    assert pair.share == 1 and pair.common == 5


@pytest.mark.parametrize(
    "excluded", ["standard", "nested", "catalog_values", "enum_props", "nested_values"]
)
def test_composition_ignores_non_composition_members(tmp_path: Path, excluded: str) -> None:
    source, target = _pair(tmp_path)
    names = ("А", "Б", "В", "Г", "Д")
    for builder, name in ((source, "Источник"), (target, "Приемник")):
        if excluded == "standard":
            builder.add("Справочник", name, _composition_props(*names, kind="Свойство"))
        elif excluded == "nested":
            builder.add(
                "Документ", name, [("ТабличнаяЧасть", "Строки", "", "", _composition_props(*names))]
            )
        elif excluded == "catalog_values":
            builder.add("Справочник", name, values=[(value, "") for value in names])
        elif excluded == "enum_props":
            builder.add("Перечисление", name, _composition_props(*names))
        else:
            builder.add(
                "Перечисление", name, values=[("Родитель", ""), *((value, "") for value in names)]
            )
            builder.conn.execute(
                "UPDATE object_values SET parent_id = (SELECT min(id) FROM object_values) "
                "WHERE name <> 'Родитель'"
            )
    found = object_candidates(source.conn, target.conn)
    assert len(found) == 2
    assert all(item.confidence is Confidence.NO_PAIR for item in found)
