"""Проверка формата правил (спецификация `rules-validation`, «Проверка формата»)."""

from pathlib import Path

import pytest

from kd2_rules_mcp.kd2.model import RulesDocument
from kd2_rules_mcp.kd2.rules_io import load_exchange_rules, load_registration_rules, load_rules
from kd2_rules_mcp.validation.format import (
    DANGLING_REF,
    DUPLICATE_CODE,
    DUPLICATE_NAME,
    REQUIRED,
    UNKNOWN_ATTR,
    UNKNOWN_TAG,
    check_format,
)
from kd2_rules_mcp.validation.report import Level, ValidationReport
from tests.corpus import CorpusFile, corpus_params

DATA = Path(__file__).parent / "data"

_EXCHANGE_HEAD = "<ПравилаОбмена><ВерсияФормата>2.01</ВерсияФормата>"
_REGISTRATION_HEAD = "<ПравилаРегистрации><ВерсияФормата>2.01</ВерсияФормата>"


def _load(xml: str) -> RulesDocument:
    return load_rules(xml.encode())


def _exchange(body: str = "") -> RulesDocument:
    return _load(f"{_EXCHANGE_HEAD}{body}</ПравилаОбмена>")


def _registration(body: str = "") -> RulesDocument:
    return _load(f"{_REGISTRATION_HEAD}{body}</ПравилаРегистрации>")


def _pko(
    code: str,
    name: str,
    order: int,
    *,
    source: str = "СправочникСсылка.А",
    target: str = "СправочникСсылка.Б",
    extra: str = "",
) -> str:
    return (
        "<Правило>"
        f"<Код>{code}</Код>"
        f"<Наименование>{name}</Наименование>"
        f"<Порядок>{order}</Порядок>"
        f"<Источник>{source}</Источник>"
        f"<Приемник>{target}</Приемник>"
        f"{extra}"
        "</Правило>"
    )


def _issues(report: ValidationReport, check: str, level: Level | None = None) -> list:
    found = [issue for issue in report.issues if issue.check == check]
    if level is not None:
        found = [issue for issue in found if issue.level is level]
    return found


def test_unknown_top_level_tag_is_error() -> None:
    """Сценарий «Неизвестный тег верхнего уровня»: ошибка с именем тега."""
    report = check_format(_exchange("<ЧужойТег>1</ЧужойТег>"))
    issues = _issues(report, UNKNOWN_TAG, Level.ERROR)
    assert len(issues) == 1
    assert "ЧужойТег" in issues[0].message
    assert issues[0].address == "ПравилаОбмена / тег ЧужойТег"


def test_duplicate_pko_code_is_error_with_both_rules() -> None:
    """Сценарий «Дубликат кода ПКО»: ошибка, в тексте оба правила."""
    body = (
        "<ПравилаКонвертацииОбъектов>"
        + _pko("Виды", "Виды операций", 50)
        + _pko("Виды", "Виды оплаты", 100)
        + "</ПравилаКонвертацииОбъектов>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, DUPLICATE_CODE, Level.ERROR)
    assert len(issues) == 1
    message = issues[0].message
    assert "Виды операций" in message
    assert "Виды оплаты" in message
    assert "№1" in message and "№2" in message
    assert "порядок 50" in message and "порядок 100" in message
    assert issues[0].address == "ПКО «Виды»"
    assert not _issues(report, DUPLICATE_CODE, Level.WARNING)


def test_group_code_equal_to_pko_is_not_duplicate() -> None:
    body = (
        "<ПравилаКонвертацииОбъектов><Группа><Код>Виды</Код>"
        + _pko("Виды", "Виды операций", 50)
        + "</Группа></ПравилаКонвертацииОбъектов>"
    )
    report = check_format(_exchange(body))
    assert _issues(report, DUPLICATE_CODE) == []


def test_skipped_pko_tag_is_warning() -> None:
    body = (
        "<ПравилаКонвертацииОбъектов>"
        + _pko("А", "Номенклатура", 1, extra="<РегистрироватьОбъектНаУзлеОтправителе/>")
        + "</ПравилаКонвертацииОбъектов>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, UNKNOWN_TAG, Level.WARNING)
    assert len(issues) == 1
    assert "РегистрироватьОбъектНаУзлеОтправителе" in issues[0].message
    assert "читатель пропустит" in issues[0].message
    assert issues[0].address == "ПКО «А» / тег РегистрироватьОбъектНаУзлеОтправителе"
    assert report.errors == []


def test_unskipped_pko_tag_is_error() -> None:
    body = (
        "<ПравилаКонвертацииОбъектов>"
        + _pko("А", "Номенклатура", 1, extra="<ЧужоеПоле>1</ЧужоеПоле>")
        + "</ПравилаКонвертацииОбъектов>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, UNKNOWN_TAG, Level.ERROR)
    assert len(issues) == 1
    assert "ЧужоеПоле" in issues[0].message
    assert issues[0].address == "ПКО «А» / тег ЧужоеПоле"


def test_reader_accepted_tag_outside_writer_schema_is_silent() -> None:
    """ПВД `ИмяТипаПриемника` и ПКО `ПоляПоиска` читатель разбирает."""
    body = (
        "<ПравилаКонвертацииОбъектов>"
        + _pko("А", "Номенклатура", 1, extra="<ПоляПоиска>Код</ПоляПоиска>")
        + "</ПравилаКонвертацииОбъектов>"
        "<ПравилаВыгрузкиДанных><Правило>"
        "<Код>А</Код><Наименование>А</Наименование><Порядок>1</Порядок>"
        "<КодПравилаКонвертации>А</КодПравилаКонвертации>"
        "<ИмяТипаПриемника>СправочникСсылка.Б</ИмяТипаПриемника>"
        "</Правило></ПравилаВыгрузкиДанных>"
    )
    report = check_format(_exchange(body))
    assert report.issues == []


def test_registration_unknown_top_level_tag_is_warning() -> None:
    report = check_format(_registration("<ЛишнийРаздел>1</ЛишнийРаздел>"))
    issues = _issues(report, UNKNOWN_TAG, Level.WARNING)
    assert len(issues) == 1
    assert "ЛишнийРаздел" in issues[0].message
    assert "читатель пропустит" in issues[0].message
    assert report.errors == []


def test_exchange_skipped_top_level_tags_are_warnings() -> None:
    report = check_format(
        _exchange("<РежимСовместимости>РежимСовместимостиСБСП21</РежимСовместимости>")
    )
    issues = _issues(report, UNKNOWN_TAG, Level.WARNING)
    assert [issue.address for issue in issues] == ["ПравилаОбмена / тег РежимСовместимости"]
    assert report.errors == []


def test_unknown_attribute_is_warning() -> None:
    body = (
        "<ПравилаКонвертацииОбъектов>"
        + _pko("А", "Номенклатура", 1).replace("<Правило>", '<Правило Лишний="1">', 1)
        + "</ПравилаКонвертацииОбъектов>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, UNKNOWN_ATTR, Level.WARNING)
    assert len(issues) == 1
    assert "Лишний" in issues[0].message
    assert "читатель пропустит" in issues[0].message
    assert issues[0].address == "ПКО «А» / атрибут Лишний"
    assert report.errors == []


def test_pks_side_unknown_tag_is_warning() -> None:
    extra = (
        "<Свойства><Свойство>"
        '<Источник Имя="ИНН" Вид="Реквизит"><Лишний>1</Лишний></Источник>'
        '<Приемник Имя="ИНН" Вид="Реквизит"/>'
        "</Свойство></Свойства>"
    )
    body = (
        "<ПравилаКонвертацииОбъектов>"
        + _pko("А", "Номенклатура", 1, extra=extra)
        + "</ПравилаКонвертацииОбъектов>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, UNKNOWN_TAG, Level.WARNING)
    assert len(issues) == 1
    assert "Лишний" in issues[0].message
    assert report.errors == []


def test_algorithm_unknown_tag_is_warning() -> None:
    body = (
        '<Алгоритмы><Алгоритм Имя="Общий">'
        "<Текст>Возврат 1;</Текст><Лишний>1</Лишний>"
        "</Алгоритм></Алгоритмы>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, UNKNOWN_TAG, Level.WARNING)
    assert len(issues) == 1
    assert issues[0].address == "алгоритм «Общий» / тег Лишний"
    assert report.errors == []


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (
            "<ПравилаКонвертацииОбъектов><Правило><Код></Код>"
            "<Наименование>Пустой</Наименование><Порядок>1</Порядок>"
            "<Источник>СправочникСсылка.А</Источник>"
            "<Приемник>СправочникСсылка.Б</Приемник>"
            "</Правило></ПравилаКонвертацииОбъектов>",
            "Пустой «Код»",
        ),
        (
            "<ПравилаКонвертацииОбъектов>"
            + _pko("А", "Номенклатура", 1, source="")
            + "</ПравилаКонвертацииОбъектов>",
            "Пустой «Источник»",
        ),
        (
            "<ПравилаКонвертацииОбъектов>"
            + _pko("А", "Номенклатура", 1, target="")
            + "</ПравилаКонвертацииОбъектов>",
            "Пустой «Приемник»",
        ),
        (
            "<ПравилаВыгрузкиДанных><Правило>"
            "<Наименование>Выгрузка</Наименование><Порядок>1</Порядок>"
            "</Правило></ПравилаВыгрузкиДанных>",
            "Пустой «Код»",
        ),
        (
            "<ПравилаОчисткиДанных><Правило><Порядок>1</Порядок></Правило></ПравилаОчисткиДанных>",
            "Пустой «Код»",
        ),
        (
            '<Алгоритмы><Алгоритм Имя=""><Текст>Возврат 1;</Текст></Алгоритм></Алгоритмы>',
            "Пустой «Имя»",
        ),
        (
            '<Запросы><Запрос Имя=""><Текст>ВЫБРАТЬ 1</Текст></Запрос></Запросы>',
            "Пустой «Имя»",
        ),
        (
            '<Параметры><Параметр Имя="" Наименование="П"/></Параметры>',
            "Пустой «Имя»",
        ),
        (
            '<Обработки><Обработка Имя="" Наименование="О">AQ==</Обработка></Обработки>',
            "Пустой «Имя»",
        ),
    ],
)
def test_empty_required_field_is_warning(body: str, fragment: str) -> None:
    report = check_format(_exchange(body))
    issues = _issues(report, REQUIRED, Level.WARNING)
    assert any(fragment in issue.message for issue in issues)
    assert report.errors == []


def test_empty_pro_code_and_metadata_are_warnings() -> None:
    body = (
        "<ПравилаРегистрацииОбъектов><Правило>"
        "<Наименование>Организации</Наименование>"
        "</Правило></ПравилаРегистрацииОбъектов>"
    )
    report = check_format(_registration(body))
    messages = [issue.message for issue in _issues(report, REQUIRED, Level.WARNING)]
    assert any("Пустой «Код»" in message for message in messages)
    assert any("Пустой «ОбъектМетаданныхИмя»" in message for message in messages)
    assert report.errors == []


def test_duplicate_algorithm_name_is_error() -> None:
    body = (
        "<Алгоритмы>"
        '<Алгоритм Имя="Общий"><Текст>Возврат 1;</Текст></Алгоритм>'
        '<Алгоритм Имя="Общий"><Текст>Возврат 2;</Текст></Алгоритм>'
        "</Алгоритмы>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, DUPLICATE_NAME, Level.ERROR)
    assert len(issues) == 1
    assert "Общий" in issues[0].message
    assert "№1" in issues[0].message and "№2" in issues[0].message


def test_duplicate_pvd_code_is_warning() -> None:
    body = (
        "<ПравилаВыгрузкиДанных>"
        "<Правило><Код>НоменклатурныеГруппы</Код>"
        "<Наименование>НоменклатурныеГруппы</Наименование><Порядок>1</Порядок></Правило>"
        "<Правило><Код>НоменклатурныеГруппы</Код>"
        "<Наименование>Аналитика затрат</Наименование><Порядок>2</Порядок></Правило>"
        "</ПравилаВыгрузкиДанных>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, DUPLICATE_CODE, Level.WARNING)
    assert len(issues) == 1
    assert "НоменклатурныеГруппы" in issues[0].message
    assert "Аналитика затрат" in issues[0].message
    assert report.errors == []


def test_duplicate_pod_code_is_warning() -> None:
    body = (
        "<ПравилаОчисткиДанных>"
        "<Правило><Код>Очистка</Код><Порядок>1</Порядок></Правило>"
        "<Правило><Код>Очистка</Код><Порядок>2</Порядок></Правило>"
        "</ПравилаОчисткиДанных>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, DUPLICATE_CODE, Level.WARNING)
    assert len(issues) == 1
    assert report.errors == []


def test_duplicate_pro_code_is_warning() -> None:
    def rule(code: str, name: str) -> str:
        return (
            "<Правило>"
            f"<Код>{code}</Код><Наименование>{name}</Наименование>"
            "<ОбъектМетаданныхИмя>Документ.А</ОбъектМетаданныхИмя>"
            "</Правило>"
        )

    body = (
        "<ПравилаРегистрацииОбъектов>"
        + rule("000000001", "Извещение")
        + rule("000000001", "Распределение")
        + "</ПравилаРегистрацииОбъектов>"
    )
    report = check_format(_registration(body))
    issues = _issues(report, DUPLICATE_CODE, Level.WARNING)
    assert len(issues) == 1
    assert "Извещение" in issues[0].message
    assert "Распределение" in issues[0].message
    assert report.errors == []


def test_dangling_pvd_pks_and_parameter_are_errors() -> None:
    body = (
        '<Параметры><Параметр Имя="П1" ПравилоКонвертации="НетТакого"/></Параметры>'
        "<ПравилаКонвертацииОбъектов>"
        + _pko(
            "А",
            "Номенклатура",
            1,
            extra=(
                "<Свойства>"
                "<Свойство><Код>1</Код><КодПравилаКонвертации>НетПКС</КодПравилаКонвертации>"
                '<Приемник Имя="ИНН" Вид="Реквизит"/></Свойство>'
                "<Группа><Код>Г</Код><Наименование>Группа</Наименование>"
                "<КодПравилаКонвертации>НетГруппы</КодПравилаКонвертации>"
                '<Приемник Имя="ТЧ" Вид="ТабличнаяЧасть"/></Группа>'
                "</Свойства>"
            ),
        )
        + "</ПравилаКонвертацииОбъектов>"
        "<ПравилаВыгрузкиДанных><Правило><Код>Выгрузка</Код><Наименование>Выгрузка</Наименование>"
        "<Порядок>1</Порядок><КодПравилаКонвертации>НетПВД</КодПравилаКонвертации>"
        "</Правило></ПравилаВыгрузкиДанных>"
    )
    report = check_format(_exchange(body))
    issues = _issues(report, DANGLING_REF, Level.ERROR)
    messages = " ".join(issue.message for issue in issues)
    assert "НетПВД" in messages
    assert "НетПКС" in messages
    assert "НетГруппы" in messages
    assert "НетТакого" in messages
    addresses = [issue.address for issue in issues]
    assert "ПВД «Выгрузка»" in addresses
    assert "параметр «П1»" in addresses
    assert any(address.startswith("ПКО «А» / ПКС ") for address in addresses)


def test_empty_conversion_code_is_not_dangling() -> None:
    body = (
        "<ПравилаКонвертацииОбъектов>"
        + _pko(
            "А",
            "Номенклатура",
            1,
            extra="<Свойства><Свойство><КодПравилаКонвертации></КодПравилаКонвертации></Свойство></Свойства>",
        )
        + "</ПравилаКонвертацииОбъектов>"
        "<ПравилаВыгрузкиДанных><Правило><Код>Выгрузка</Код><Порядок>1</Порядок>"
        "<КодПравилаКонвертации></КодПравилаКонвертации></Правило></ПравилаВыгрузкиДанных>"
    )
    report = check_format(_exchange(body))
    assert _issues(report, DANGLING_REF) == []


def test_pod_name_tag_accepted_by_reader_is_silent() -> None:
    body = (
        "<ПравилаОчисткиДанных><Правило><Код>Очистка</Код><Наименование>Очистка</Наименование>"
        "<Порядок>1</Порядок><УдалятьЗаПериод>Год</УдалятьЗаПериод></Правило></ПравилаОчисткиДанных>"
    )
    report = check_format(_exchange(body))
    assert report.issues == []


def test_exchange_sample_has_no_issues() -> None:
    report = check_format(load_exchange_rules(DATA / "exchange_rules.xml"))
    assert report.issues == []


def test_registration_sample_has_no_issues() -> None:
    report = check_format(load_registration_rules(DATA / "registration_rules.xml"))
    assert report.issues == []


@pytest.mark.corpus
@pytest.mark.parametrize("item", corpus_params())
def test_corpus_format_has_no_errors(item: CorpusFile) -> None:
    report = check_format(load_rules(item.path))
    preview = "; ".join(f"{issue.address}: {issue.message}" for issue in report.errors[:5])
    assert report.errors == [], f"{item.id}: {preview}"
