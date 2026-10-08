"""Каноническая форма XML правил КД 2 для смыслового сравнения.

Два файла правил считаются равными по смыслу, если равны их канонические формы. Форма
строится прямо по XML, без модели правил, чтобы проверять экспорт независимо от него.
Строки ниже: `ВК` — `reference/kd2-cfg/DataProcessors/ВыгрузкаКонвертации/Ext/ObjectModule.bsl`,
`БСП` — читатель `DataProcessors/КонвертацияОбъектовИнформационныхБаз` (БСП 3.1.12).

Правила канонизации (каждое покрыто тестом в `tests/test_canonical.py`):

- К1. Порядок атрибутов не важен.
- К2. Пробельный текст между элементами (отступы, переводы строк) не значим. Непробельный
  текст между элементами (маркеры доработок `//bt_N` … `//bt_K`) значим: сравнивается без
  пробелов по краям и на своём месте среди элементов списка. Текст листовых элементов
  сравнивается как есть, включая пробелы по краям.
- К3. Переводы строк в тексте и атрибутах приводятся к LF (`CRLF` и одиночный `CR` → `LF`).
- К4. Элемент без атрибутов, без дочерних элементов и с пустым или пробельным текстом равен
  отсутствующему: писатель КД такие значения не выводит (`ДобавитьЭлемент` ВК:83-95,
  `ПустаяСтрока` ВК:750-758), а читатель БСП отсутствующий тег читает как пустое значение.
- К5. Флаг-элемент со значением `false` равен отсутствующему для флагов, которые писатель
  выводит «только если истина» (`ВыгрузитьБулевоЗначениеТолькоЕслиИстина`, ВК:740-748).
- К6. Атрибуты-флаги `Отключить`, `Поиск`, `Обязательное` со значением `false` равны
  отсутствующим (у ПКС писатель выводит их только при истине, ВК:619-629 и 693-695).
- К7. Числовые `Порядок` и `ПриводитьКДлине` со значением `0` равны отсутствующим:
  `ДобавитьЭлемент` не выводит незаполненное число (ВК:83-95).
- К8. Порядок простых полей записи не важен: читатель БСП разбирает поля правила в цикле по
  имени узла (например, `ЗагрузитьПравилоКонвертации`, БСП:5751). Порядок элементов списков
  (`LIST_TAGS`: правила, группы, свойства, значения, параметры …) важен и сохраняется.

Комментарии и инструкции обработки в канонической форме не участвуют.
"""

from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from kd_rules_mcp.errors import RulesFormatError

# К5: флаги «только если истина» (ВК, строки в комментариях).
FALSE_DEFAULT_FLAG_TAGS: frozenset[str] = frozenset(
    {
        "УдалятьСопоставленныеОбъектыВПриемникеПриИхУдаленииВИсточнике",  # 410
        "НеЗамещать",  # 631, 705, 819
        "ПолучитьИзВходящихДанных",  # 641, 711
        "ПоискПоДатеНаРавенство",  # 683
        "ВыгружатьГруппуЧерезФайл",  # 712
        "НеЗапоминатьВыгруженные",  # 820
        "СинхронизироватьПоИдентификатору",  # 821
        "ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли",  # 822
        "НеВыгружатьОбъектыСвойствПоСсылкам",  # 823
        "НеСоздаватьЕслиНеНайден",  # 824
        "ИспользоватьБыстрыйПоискПриЗагрузке",  # 825
        "ГенерироватьНовыйНомерИлиКодЕслиНеУказан",  # 826
        "ВыгружатьОбъектТолькоПриНаличииНаНегоСсылки",  # 827
        "ПриПереносеОбъектаПоСсылкеУстанавливатьТолькоGIUD",  # 828
        "НеЗамещатьОбъектСозданныйВИнформационнойБазеПриемнике",  # 829
        "ВыбиратьДанныеДляВыгрузкиОднимЗапросом",  # 923
        "НеВыгружатьОбъектыСозданныеВБазеПриемнике",  # 924
    }
)

# К6: атрибуты-флаги со значением по умолчанию «ложь».
FALSE_DEFAULT_FLAG_ATTRS: frozenset[str] = frozenset({"Отключить", "Поиск", "Обязательное"})

# К7: числовые теги, которые писатель не выводит при нуле.
ZERO_DEFAULT_NUMBER_TAGS: frozenset[str] = frozenset({"Порядок", "ПриводитьКДлине"})

# К8: теги элементов списков — их порядок значим; остальные дочерние теги — поля записи.
LIST_TAGS: frozenset[str] = frozenset(
    {
        "Правило",
        "Группа",
        "Свойство",
        "Значение",
        "Алгоритм",
        "Запрос",
        "Параметр",
        "Обработка",
        "ВариантПоиска",
        "Элемент",
        "ЭлементОтбора",
    }
)

# Псевдотег текстового фрагмента между элементами (К2).
TEXT_TAG = "#текст"


@dataclass(frozen=True, slots=True)
class CanonicalElement:
    """Элемент канонической формы: тег, атрибуты (отсортированы), текст, дочерние элементы."""

    tag: str
    attrs: tuple[tuple[str, str], ...]
    text: str
    children: tuple["CanonicalElement", ...]


def normalize_newlines(value: str) -> str:
    """Переводы строк к LF: `CRLF` и одиночный `CR` (правило К3)."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def is_default_value(tag: str, text: str) -> bool:
    """Равен ли листовой элемент без атрибутов отсутствующему (правила К4, К5, К7)."""
    if not text.strip():
        return True
    if tag in FALSE_DEFAULT_FLAG_TAGS and text == "false":
        return True
    return tag in ZERO_DEFAULT_NUMBER_TAGS and text == "0"


# Прежние имена внутренних помощников.
_normalize_newlines = normalize_newlines
_is_default = is_default_value


def _text_fragment(value: str | None) -> "CanonicalElement | None":
    stripped = _normalize_newlines(value or "").strip()
    return CanonicalElement(TEXT_TAG, (), stripped, ()) if stripped else None


def _canonicalize(element: etree._Element) -> CanonicalElement | None:
    tag = str(element.tag)
    attrs = tuple(
        sorted(
            (str(name), _normalize_newlines(str(value)))
            for name, value in element.attrib.items()
            if not (name in FALSE_DEFAULT_FLAG_ATTRS and value == "false")
        )
    )
    elements = [c for c in element if isinstance(c.tag, str)]
    if not elements:
        # Дочерними здесь могут быть только комментарии: их текст пропускаем, хвосты — нет.
        text = (element.text or "") + "".join(child.tail or "" for child in element)
        text = _normalize_newlines(text)
        if not attrs and _is_default(tag, text):
            return None
        return CanonicalElement(tag, attrs, text, ())

    fields: list[CanonicalElement] = []
    sequence: list[CanonicalElement] = []
    fragment = _text_fragment(element.text)
    if fragment:
        sequence.append(fragment)
    for child in element:
        if isinstance(child.tag, str):
            canonical = _canonicalize(child)
            if canonical is not None:
                (sequence if canonical.tag in LIST_TAGS else fields).append(canonical)
        fragment = _text_fragment(child.tail)
        if fragment:
            sequence.append(fragment)
    fields.sort(key=lambda item: item.tag)
    children = tuple(fields + sequence)
    if not attrs and not children:
        return None
    return CanonicalElement(tag, attrs, "", children)


def parse_xml(source: bytes | str | Path) -> etree._Element:
    """Разбирает XML правил без разрешения внешних сущностей."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    try:
        if isinstance(source, Path):
            return etree.parse(str(source), parser).getroot()
        data = source.encode("utf-8") if isinstance(source, str) else source
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError as error:
        raise RulesFormatError(f"Файл правил не является корректным XML: {error}") from error


def canonical_form(source: bytes | str | Path | etree._Element) -> CanonicalElement:
    """Каноническая форма XML-документа правил."""
    root = source if isinstance(source, etree._Element) else parse_xml(source)
    canonical = _canonicalize(root)
    if canonical is None:
        return CanonicalElement(str(root.tag), (), "", ())
    return canonical


def _describe(element: CanonicalElement) -> str:
    code = next((c.text for c in element.children if c.tag in ("Код", "Имя")), None)
    name = dict(element.attrs).get("Имя")
    key = code or name
    return f"{element.tag}[{key}]" if key else element.tag


def _shorten(value: str, limit: int = 80) -> str:
    value = value.replace("\n", "⏎")
    return value if len(value) <= limit else value[:limit] + "…"


def _diff(
    left: CanonicalElement, right: CanonicalElement, path: str, out: list[str], limit: int
) -> None:
    if len(out) >= limit:
        return
    if left.tag != right.tag:
        out.append(f"{path}: тег {left.tag!r} ≠ {right.tag!r}")
        return
    if left.attrs != right.attrs:
        out.append(f"{path}: атрибуты {dict(left.attrs)} ≠ {dict(right.attrs)}")
    if left.text != right.text:
        out.append(f"{path}: текст {_shorten(left.text)!r} ≠ {_shorten(right.text)!r}")
    for index, (a, b) in enumerate(zip(left.children, right.children, strict=False)):
        _diff(a, b, f"{path}/{_describe(a)}#{index}", out, limit)
    if len(left.children) != len(right.children):
        common = min(len(left.children), len(right.children))
        extra_left = [_describe(c) for c in left.children[common:]]
        extra_right = [_describe(c) for c in right.children[common:]]
        out.append(
            f"{path}: число дочерних {len(left.children)} ≠ {len(right.children)};"
            f" лишние слева {extra_left[:5]}, справа {extra_right[:5]}"
        )


def canonical_diff(left: CanonicalElement, right: CanonicalElement, limit: int = 20) -> list[str]:
    """Расхождения двух канонических форм (не больше `limit`), пустой список — равны."""
    out: list[str] = []
    _diff(left, right, _describe(left), out, limit)
    return out[:limit]
