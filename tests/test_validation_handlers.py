"""Вынос обработчиков в BSL-обёртки (спецификация ``rules-validation``,
«Подготовка кода обработчиков к проверке»)."""

import os
from pathlib import Path

import pytest

from kd2_rules_mcp.kd2.model import ExchangeRules
from kd2_rules_mcp.kd2.rules_io import load_exchange_rules
from kd2_rules_mcp.kd2.schema import CONVERSION_EVENTS
from kd2_rules_mcp.validation.address import pks_address, rule_address, walk_pks
from kd2_rules_mcp.validation.handlers import (
    ALGORITHM_EVENT,
    EVENT_AREAS,
    HANDLER_PARAMS,
    HandlerExport,
    HandlerFile,
    export_handlers,
    locate,
)
from tests.corpus import EXCHANGE_KINDS, CorpusFile, corpus_params

# Каталог XML-выгрузки конфигурации «Конвертация данных 2.1» (эталон формата; выгружается
# из своей базы КД Конфигуратором «в файлы»). Без переменной сверка с макетом пропускается.
REFERENCE_ENV = "KD2_REFERENCE_DIR"
PARAMS_TEMPLATE = Path(
    "DataProcessors",
    "ВыгрузкаОбработчиков",
    "Templates",
    "ПараметрыОбработчиков",
    "Ext",
    "Template.txt",
)

MODULE_VARIABLES = (
    "Перем Параметры;",
    "Перем Алгоритмы;",
    "Перем Запросы;",
    "Перем УзелДляОбмена;",
    "Перем ОбщиеПроцедурыФункции;",
)

PKS_BODY = "А = 1;\nБ = 2;\nНесуществующаяПроцедура();\nВ = 4;"
ERROR_LINE = 3


def areas_from_template(path: Path) -> dict[str, tuple[str, ...]]:
    """Области макета параметров: имя и список параметров через запятую."""
    areas: dict[str, tuple[str, ...]] = {}
    name: str | None = None
    chunks: list[str] = []

    def flush() -> None:
        if name is None:
            return
        params = tuple(part.strip() for part in "\n".join(chunks).split(",") if part.strip())
        areas[name] = params

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("#Область "):
            flush()
            name = line.removeprefix("#Область ").strip()
            chunks = []
        else:
            chunks.append(line)
    flush()
    return areas


def test_handler_params_match_template() -> None:
    """Таблица параметров совпадает с разбором макета ВыгрузкаОбработчиков."""
    reference = os.environ.get(REFERENCE_ENV, "").strip()
    if not reference:
        pytest.skip(f"Эталон КД не задан: переменная {REFERENCE_ENV} пуста")
    template = Path(reference) / PARAMS_TEMPLATE
    if not template.is_file():
        pytest.skip(f"В выгрузке эталона КД нет макета {template}")
    parsed = areas_from_template(template)
    assert parsed == HANDLER_PARAMS
    assert set(EVENT_AREAS.values()) == set(HANDLER_PARAMS)


def test_event_areas_cover_model_tags() -> None:
    """Каждый тег обработчика модели сопоставлен области макета."""
    expected = {("exchange_rules", tag) for tag in CONVERSION_EVENTS}
    expected |= {
        ("pko", tag)
        for tag in (
            "ПередВыгрузкой",
            "ПриВыгрузке",
            "ПослеВыгрузки",
            "ПослеВыгрузкиВФайл",
            "ПередЗагрузкой",
            "ПриЗагрузке",
            "ПослеЗагрузки",
            "ПоследовательностьПолейПоиска",
        )
    }
    expected |= {("pks", tag) for tag in ("ПередВыгрузкой", "ПриВыгрузке", "ПослеВыгрузки")}
    expected |= {
        ("pks_group", tag)
        for tag in (
            "ПередОбработкойВыгрузки",
            "ПередВыгрузкой",
            "ПриВыгрузке",
            "ПослеВыгрузки",
            "ПослеОбработкиВыгрузки",
        )
    }
    expected |= {
        ("pvd", tag)
        for tag in (
            "ПередОбработкойПравила",
            "ПередВыгрузкойОбъекта",
            "ПослеВыгрузкиОбъекта",
            "ПослеОбработкиПравила",
        )
    }
    expected |= {
        ("pod", tag)
        for tag in ("ПередОбработкойПравила", "ПослеОбработкиПравила", "ПередУдалениемОбъекта")
    }
    expected.add(("parameter", "ПослеЗагрузкиПараметра"))
    assert set(EVENT_AREAS) == expected


def rules_xml() -> bytes:
    """Маленькие правила: по обработчику каждого вида и заведомо пустые тексты."""
    return f"""<ПравилаОбмена>
<ВерсияФормата>2.01</ВерсияФормата>
<Источник>Источник</Источник>
<Приемник>Приемник</Приемник>
<ПередВыгрузкойДанных>Отказ = Ложь;</ПередВыгрузкойДанных>
<ПослеЗагрузкиДанных>Счетчик = 1;</ПослеЗагрузкиДанных>
<ПослеЗагрузкиПравилОбмена>   </ПослеЗагрузкиПравилОбмена>
<Параметры>
<Параметр Имя="Курс" ПослеЗагрузкиПараметра="Значение = 1;"/>
<Параметр Имя="Пустой" ПослеЗагрузкиПараметра="   "/>
</Параметры>
<ПравилаКонвертацииОбъектов>
<Правило>
<Код>Ном</Код>
<ПередВыгрузкой>А = 1;</ПередВыгрузкой>
<ПриВыгрузке></ПриВыгрузке>
<Свойства>
<Группа>
<Код>Г</Код>
<Источник Имя="Товары" Вид="ТабличнаяЧасть"/>
<Приемник Имя="Товары" Вид="ТабличнаяЧасть"/>
<ПередОбработкойВыгрузки>Отказ = Ложь;</ПередОбработкойВыгрузки>
<Свойство>
<Код>1</Код>
<Источник Имя="Количество" Вид="Реквизит"/>
<Приемник Имя="Количество" Вид="Реквизит"/>
<ПередВыгрузкой>{PKS_BODY}</ПередВыгрузкой>
<ПриВыгрузке>   </ПриВыгрузке>
</Свойство>
</Группа>
<Свойство>
<Код>2</Код>
<Источник Имя="Артикул" Вид="Реквизит"/>
<Приемник Имя="Артикул" Вид="Реквизит"/>
<ПослеВыгрузки>Значение = Значение;</ПослеВыгрузки>
</Свойство>
</Свойства>
</Правило>
</ПравилаКонвертацииОбъектов>
<ПравилаВыгрузкиДанных>
<Правило>
<Код>В1</Код>
<ПередОбработкойПравила>ВыборкаДанных = Неопределено;</ПередОбработкойПравила>
</Правило>
</ПравилаВыгрузкиДанных>
<ПравилаОчисткиДанных>
<Правило>
<Код>О1</Код>
<ПередУдалениемОбъекта>Отказ = Истина;</ПередУдалениемОбъекта>
</Правило>
</ПравилаОчисткиДанных>
<Алгоритмы>
<Алгоритм Имя="Сложить">
<Текст>Возврат А + Б;</Текст>
<Параметры>А, Б</Параметры>
</Алгоритм>
<Алгоритм Имя="ПустойАлгоритм"><Текст>   </Текст></Алгоритм>
</Алгоритмы>
</ПравилаОбмена>""".encode()


def lines_of(path: Path) -> list[str]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    payload = raw.removeprefix(b"\xef\xbb\xbf")
    assert b"\r\n" in payload
    assert b"\n" not in payload.replace(b"\r\n", b"")
    return payload.decode("utf-8").splitlines()


def file_of(export: HandlerExport, address: str, event: str) -> HandlerFile:
    matches = [item for item in export.files if item.address == address and item.event == event]
    assert len(matches) == 1
    return matches[0]


def signature(lines: list[str], body_start: int) -> tuple[str, tuple[str, ...]]:
    header = lines[: body_start - 1]
    start = max(index for index, line in enumerate(header) if line.startswith("Процедура "))
    text = "\n".join(header[start : body_start - 1])
    head, rest = text.split("(", 1)
    inside = rest.rsplit(")", 1)[0]
    params = (
        tuple(part.strip() for part in inside.split(",") if part.strip()) if inside.strip() else ()
    )
    return head.removeprefix("Процедура ").strip(), params


def test_wrappers_cover_every_kind(tmp_path: Path) -> None:
    """Обработчики всех видов попадают в файлы; пустой текст файла не даёт; адреса верные."""
    rules = load_exchange_rules(rules_xml())
    alien = tmp_path / "чужой.txt"
    alien.write_text("не трогать", encoding="utf-8")
    exported = export_handlers(rules, tmp_path)
    export_handlers(rules, tmp_path)
    assert alien.read_text(encoding="utf-8") == "не трогать"

    expected = {
        (rule_address(rules.root), "ПередВыгрузкойДанных"),
        (rule_address(rules.root), "ПослеЗагрузкиДанных"),
        ("параметр «Курс»", "ПослеЗагрузкиПараметра"),
        ("ПКО «Ном»", "ПередВыгрузкой"),
        ("ПКО «Ном» / ПКС Товары", "ПередОбработкойВыгрузки"),
        ("ПКО «Ном» / ПКС Товары/Количество", "ПередВыгрузкой"),
        ("ПКО «Ном» / ПКС Артикул", "ПослеВыгрузки"),
        ("ПВД «В1»", "ПередОбработкойПравила"),
        ("ПОД «О1»", "ПередУдалениемОбъекта"),
        ("алгоритм «Сложить»", ALGORITHM_EVENT),
    }
    assert {(item.address, item.event) for item in exported.files} == expected

    samples = {
        ("exchange_rules", "ПередВыгрузкойДанных"): "Конвертация_ПередВыгрузкойДанных",
        ("exchange_rules", "ПослеЗагрузкиДанных"): "Конвертация_ПослеЗагрузкиДанных",
        ("pko", "ПередВыгрузкой"): "ПКО_Ном_ПередВыгрузкойОбъекта",
        ("pks_group", "ПередОбработкойВыгрузки"): "ПКГС_Ном_Товары_ПередОбработкойВыгрузки_Г_3",
        ("pks", "ПередВыгрузкой"): "ПКС_Ном_Товары_Количество_ПередВыгрузкойСвойства_1_3",
        ("pks", "ПослеВыгрузки"): "ПКС_Ном_Артикул_ПослеВыгрузкиСвойства_2_3",
        ("pvd", "ПередОбработкойПравила"): "ПВД_В1_ПередОбработкойПравила",
        ("pod", "ПередУдалениемОбъекта"): "ПОД_О1_ПередУдалениемОбъекта",
        ("parameter", "ПослеЗагрузкиПараметра"): "Параметры_Курс_ПослеЗагрузкиПараметра",
    }
    # Одно событие «ПередВыгрузкой» есть и у ПКО, и у ПКС — имена сверяем по адресу.
    named = {
        "ПКО «Ном»": samples[("pko", "ПередВыгрузкой")],
        "ПКО «Ном» / ПКС Товары": samples[("pks_group", "ПередОбработкойВыгрузки")],
        "ПКО «Ном» / ПКС Товары/Количество": samples[("pks", "ПередВыгрузкой")],
        "ПКО «Ном» / ПКС Артикул": samples[("pks", "ПослеВыгрузки")],
        "ПВД «В1»": samples[("pvd", "ПередОбработкойПравила")],
        "ПОД «О1»": samples[("pod", "ПередУдалениемОбъекта")],
        "параметр «Курс»": samples[("parameter", "ПослеЗагрузкиПараметра")],
        rule_address(rules.root): {
            "ПередВыгрузкойДанных": samples[("exchange_rules", "ПередВыгрузкойДанных")],
            "ПослеЗагрузкиДанных": samples[("exchange_rules", "ПослеЗагрузкиДанных")],
        },
    }
    for item in exported.files:
        file_lines = lines_of(item.path)
        assert all(variable in file_lines for variable in MODULE_VARIABLES)
        procedure, params = signature(file_lines, item.body_start)
        assert file_lines[item.body_end] == "КонецПроцедуры"
        body = file_lines[item.body_start - 1 : item.body_end]
        assert "ВыгрузитьРегистр" not in body
        assert any(line.startswith("Процедура ВыгрузитьРегистр(") for line in file_lines)
        if item.event == ALGORITHM_EVENT:
            assert procedure == "Сложить"
            assert params == ("А", "Б")
            assert item.path.name == "Алгоритм_Сложить.bsl"
        else:
            area = EVENT_AREAS[_kind_of(item.address, item.event), item.event]
            assert params == HANDLER_PARAMS[area]
            expect = named[item.address]
            assert procedure == (expect if isinstance(expect, str) else expect[item.event])
        assert body == _model_text(rules, item.address, item.event).splitlines()


def test_repeat_export_removes_cleared_handler(tmp_path: Path) -> None:
    """Обработчик, очищенный в правилах, не остаётся файлом; соседний файл на месте."""
    rules = load_exchange_rules(rules_xml())
    first = export_handlers(rules, tmp_path)
    kept = file_of(first, rule_address(rules.root), "ПередВыгрузкойДанных")
    algorithm = next(item for item in rules.algorithms() if item.code == "Сложить")
    dropped = file_of(first, rule_address(algorithm), ALGORITHM_EVENT)
    algorithm.values["Текст"] = ""
    again = export_handlers(rules, tmp_path)
    assert not dropped.path.exists()
    assert kept.path.is_file()
    assert again.removed == (dropped.name,)


def test_export_leaves_foreign_files(tmp_path: Path) -> None:
    """Чужой .bsl, текст не .bsl и обёртка в подпапке после выгрузки не меняются."""
    foreign = tmp_path / "чужой.bsl"
    foreign.write_text("Это не обёртка", encoding="utf-8")
    notes = tmp_path / "заметки.txt"
    notes.write_text("заметка", encoding="utf-8")
    nested = tmp_path / "вложенная"
    nested.mkdir()
    nested_bsl = nested / "старый.bsl"
    nested_bsl.write_bytes(("\r\n".join(MODULE_VARIABLES) + "\r\n").encode("utf-8-sig"))
    before = {
        foreign: foreign.read_bytes(),
        notes: notes.read_bytes(),
        nested_bsl: nested_bsl.read_bytes(),
    }
    export_handlers(load_exchange_rules(rules_xml()), tmp_path)
    for path, payload in before.items():
        assert path.read_bytes() == payload


def test_repeat_export_without_changes_removes_nothing(tmp_path: Path) -> None:
    rules = load_exchange_rules(rules_xml())
    first = export_handlers(rules, tmp_path)
    names = {item.path.name for item in first.files}
    again = export_handlers(rules, tmp_path)
    assert again.removed == ()
    assert {item.path.name for item in again.files} == names
    assert all((tmp_path / name).is_file() for name in names)


def _kind_of(address: str, event: str) -> str:
    if address.startswith("ПКО ") and " / ПКС " in address and event == "ПередОбработкойВыгрузки":
        return "pks_group"
    if address.startswith("ПКО ") and " / ПКС " in address:
        return "pks"
    if address.startswith("ПКО "):
        return "pko"
    if address.startswith("ПВД "):
        return "pvd"
    if address.startswith("ПОД "):
        return "pod"
    if address.startswith("параметр "):
        return "parameter"
    return "exchange_rules"


def _model_text(rules: ExchangeRules, address: str, event: str) -> str:
    for item_address, item_event, text in model_handlers(rules):
        if item_address == address and item_event == event:
            return text
    raise AssertionError((address, event))


def test_locate_finds_pks_handler_line(tmp_path: Path) -> None:
    """Сценарий «Ошибка в обработчике»: строка N тела — ПКО, событие и N; заголовок — пусто."""
    rules = load_exchange_rules(rules_xml())
    exported = export_handlers(rules, tmp_path)
    address = pks_address("Ном", "Товары/Количество")
    item = file_of(exported, address, "ПередВыгрузкой")
    file_lines = lines_of(item.path)
    assert file_lines[item.body_start + ERROR_LINE - 2] == "НесуществующаяПроцедура();"
    found = locate(exported, item.name, item.body_start + ERROR_LINE - 1)
    assert found is not None
    assert found.address == address
    assert found.address.startswith("ПКО ")
    assert found.event == "ПередВыгрузкой"
    assert found.line == ERROR_LINE
    assert found.line == (item.body_start + ERROR_LINE - 1) - item.line_offset
    assert locate(exported, item.name, item.body_start - 1) is None
    assert locate(exported, item.name, 1) is None
    assert locate(exported, item.name, item.body_end + 1) is None
    assert locate(exported, "нет_такого.bsl", item.body_start) is None


def model_handlers(rules: ExchangeRules) -> list[tuple[str, str, str]]:
    """Непустые обработчики и алгоритмы модели: адрес, событие, текст."""
    found: list[tuple[str, str, str]] = []

    def take(kind: str, node, address: str, *, attribute: bool = False) -> None:
        for (item_kind, tag), _area in EVENT_AREAS.items():
            if item_kind != kind:
                continue
            raw = node.attrs.get(tag) if attribute else node.values.get(tag)
            text = raw if isinstance(raw, str) else ""
            if text.strip():
                found.append((address, tag, text))

    take("exchange_rules", rules.root, rule_address(rules.root))
    parameters = rules.root.child("Параметры")
    if parameters is not None:
        for item in parameters.items:
            if item.kind.name == "parameter":
                take("parameter", item, rule_address(item), attribute=True)
    for pko in rules.pko():
        take("pko", pko, rule_address(pko))
        container = pko.child("Свойства")
        if container is None:
            continue
        for path, node in walk_pks(container):
            kind = "pks_group" if node.kind.name == "pks_group" else "pks"
            take(kind, node, pks_address(pko.code, path))
    for pvd in rules.pvd():
        take("pvd", pvd, rule_address(pvd))
    for pod in rules.pod():
        take("pod", pod, rule_address(pod))
    for algorithm in rules.algorithms():
        text = algorithm.values.get(ALGORITHM_EVENT, "")
        if isinstance(text, str) and text.strip():
            found.append((rule_address(algorithm), ALGORITHM_EVENT, text))
    return found


@pytest.mark.corpus
@pytest.mark.parametrize("corpus_file", corpus_params(EXCHANGE_KINDS))
def test_corpus_handlers_match_model(corpus_file: CorpusFile, tmp_path: Path) -> None:
    """Макеты корпуса: выгружены все непустые обработчики, тело совпадает с моделью."""
    rules = load_exchange_rules(corpus_file.path)
    expected = model_handlers(rules)
    exported = export_handlers(rules, tmp_path)
    assert len(exported.files) == len(expected)
    actual: list[tuple[str, str, str]] = []
    for item in exported.files:
        file_lines = lines_of(item.path)
        procedure_at = [
            index for index, line in enumerate(file_lines) if line.startswith("Процедура ")
        ]
        assert procedure_at
        assert file_lines[item.body_start - 2].rstrip().endswith("Экспорт")
        assert file_lines[item.body_end] == "КонецПроцедуры"
        body = file_lines[item.body_start - 1 : item.body_end]
        actual.append((item.address, item.event, "\n".join(body)))
        opens = sum(line.startswith(("Процедура ", "Функция ")) for line in file_lines)
        closes = sum(line.startswith(("КонецПроцедуры", "КонецФункции")) for line in file_lines)
        assert opens == closes
        assert opens >= 1
    assert sorted(actual) == sorted(
        (address, event, "\n".join(text.splitlines())) for address, event, text in expected
    )
