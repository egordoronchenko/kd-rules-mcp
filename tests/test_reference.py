"""Справка `reference_get` и поиск `reference_search` (спецификация `reference-help`)."""

import re
import shutil
import sys
from pathlib import Path

import pytest

from kd_rules_mcp.errors import Kd2Error, ReferenceUnavailableError
from kd_rules_mcp.reference import SKILL_NAME_RE, resolve_root
from kd_rules_mcp.reference.search import blocks, stem, terms
from kd_rules_mcp.reference.sections import (
    SECTIONS,
    codes,
    numbered,
    read_lines,
    spans,
    table_rows,
)
from kd_rules_mcp.service import Kd2Service, Settings

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude" / "skills"
sys.path.insert(0, str(ROOT / "scripts"))

import our_skills  # noqa: E402 — скрипт из scripts/, не пакет

# Ссылка на строку кода БСП: `стр. N` (модуль назван в шапке файла) или `БСП:N`.
BSP_REF = re.compile(r"стр\. \d+|БСП:\d+")


@pytest.fixture
def service(tmp_path: Path) -> Kd2Service:
    return Kd2Service(Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace"))


def _rows(result: dict[str, object]) -> list[str]:
    tables = result["tables"]
    assert isinstance(tables, list)
    return [row["text"] for table in tables for row in table["rows"]]


def _hits(result: dict[str, object]) -> list[str]:
    items = result["items"]
    assert isinstance(items, list)
    return [item["snippet"] for item in items]


# --- Разделы ------------------------------------------------------------------------------


def test_get_lists_sections(service: Kd2Service) -> None:
    result = service.reference_get()
    assert [item["section"] for item in result["items"]] == [
        "protocol",
        "symptoms",
        "phases",
        "handler_params",
        "pko",
        "pks",
        "pvd",
        "pro",
        "search",
    ]
    for item in result["items"]:
        assert item["title"] and item["headings"] and item["lines"] > 0
        assert (SKILLS / item["file"]).is_file()


def test_get_section_pages(service: Kd2Service) -> None:
    result = service.reference_get("protocol", offset=0, limit=10)
    lines = (SKILLS / result["file"]).read_text(encoding="utf-8").splitlines()
    assert result["parts"] == [{"line": 1, "text": "\n".join(lines[:10])}]
    assert result["total"] == len(lines)
    assert result["has_more"] is True
    tail = service.reference_get("protocol", offset=len(lines) - 2, limit=10)
    assert tail["has_more"] is False
    assert tail["parts"][0]["line"] == len(lines) - 1


def test_get_section_of_several_headings(service: Kd2Service) -> None:
    result = service.reference_get("phases", limit=200)
    text = "\n".join(part["text"] for part in result["parts"])
    assert "## Где и когда вызывается" in text
    assert "## Исключение в обработчике" in text
    assert "## Алгоритмы" not in text
    assert "## Ключевые переменные по фазам" not in text
    assert len(result["parts"]) == 2  # два раздела файла не подряд


def test_get_handler_params_points_to_handlers_context(service: Kd2Service) -> None:
    result = service.reference_get("handler_params", limit=200)
    text = "\n".join(part["text"] for part in result["parts"])
    assert result["see"] == "handlers_context"
    assert "## Переменные, доступные везде" in text
    assert "## Ключевые переменные по фазам" in text


def test_get_unknown_section(service: Kd2Service) -> None:
    allowed = "protocol, symptoms, phases, handler_params, pko, pks, pvd, pro, search"
    with pytest.raises(Kd2Error, match=allowed):
        service.reference_get("grabli")


# --- Разделы по видам правил: ключ — заголовок --------------------------------------------


@pytest.mark.parametrize("section", ["pko", "pks", "pvd", "pro", "search"])
def test_rule_kind_section_is_the_whole_skill_file(service: Kd2Service, section: str) -> None:
    result = service.reference_get(section, limit=200)
    assert result["file"] == f"kd2-rules-build/references/{section}.md"
    lines = (SKILLS / result["file"]).read_text(encoding="utf-8").splitlines()
    assert result["total"] == len(lines)
    assert result["parts"][0]["line"] == 1
    assert result["parts"][0]["text"].splitlines()[0] == lines[0]


def test_rule_kind_key_selects_heading_with_nested(service: Kd2Service) -> None:
    result = service.reference_get("pko", key="флаги", limit=200)
    # Вложенный `###` совпал сам, но его строки уже в разделе `##` — назван, не повторён.
    assert result["headings"] == [
        "Как флаг доходит до приёмника",
        "Флаги",
        "Флаги КД, которые БСП 3.1.12 не исполняет",
    ]
    assert len(result["parts"]) == 2
    text = result["parts"][1]["text"]
    assert text.startswith("## Флаги\n")
    assert text.count("### Флаги КД, которые БСП 3.1.12 не исполняет") == 1
    assert "## Связи ПКО с другими правилами" not in text
    lines = (SKILLS / result["file"]).read_text(encoding="utf-8").splitlines()
    first = result["parts"][1]["line"]
    assert lines[first - 1] == "## Флаги"
    assert result["total"] == sum(len(part["text"].split("\n")) for part in result["parts"])


def test_rule_kind_key_exact_does_not_hide_stem_matches(service: Kd2Service) -> None:
    # Ревью 26.10.2026: «флаг» целым словом совпадал только с «Как флаг…», раздел «Флаги» пропадал.
    flags = service.reference_get("pko", key="флаг", limit=200)
    assert flags["headings"][:2] == ["Как флаг доходит до приёмника", "Флаги"]
    search = service.reference_get("search", key="поиск", limit=1)
    assert "Рецепт: ключи поиска" in search["headings"]
    assert len(search["headings"]) == 3


def test_rule_kind_short_key_matches_whole_words_only(service: Kd2Service) -> None:
    # Основа короче трёх букв — только целым словом: «по» и «к» не находят «поиск» и «КД».
    assert service.reference_get("search", key="по")["total"] == 0
    assert service.reference_get("pko", key="к")["total"] == 0


def test_rule_kind_key_by_stem_and_level_three(service: Kd2Service) -> None:
    # «отбора» — не целое слово заголовка: второй проход по основе находит «Отборы в …».
    result = service.reference_get("pro", key="отбора registration_build", limit=200)
    assert result["headings"] == ["Отборы в `registration_build`"]
    text = result["parts"][0]["text"]
    assert text.startswith("### Отборы в `registration_build`")
    assert "### Ручная правка файла" not in text


def test_rule_kind_key_pages(service: Kd2Service) -> None:
    whole = service.reference_get("pko", key="флаги", limit=200)
    page = service.reference_get("pko", key="флаги", offset=5, limit=3)
    assert page["total"] == whole["total"] and page["has_more"] is True
    expected = whole["parts"][0]["text"].splitlines()[5:8]
    start = whole["parts"][0]["line"] + 5
    assert page["parts"] == [{"line": start, "text": "\n".join(expected)}]


def test_rule_kind_key_not_found_lists_headings(service: Kd2Service) -> None:
    result = service.reference_get("pvd", key="ракета")
    assert result["parts"] == [] and result["total"] == 0
    assert result["headings"] == []
    assert "Поля ПВД" in result["outline"]
    assert "reference_search" in result["hint"]


def test_rule_kind_heading_inside_code_block_is_ignored(tmp_path: Path) -> None:
    copy = tmp_path / "skills"
    shutil.copytree(SKILLS, copy)
    pvd = copy / "kd2-rules-build" / "references" / "pvd.md"
    fenced = (
        "\n```\n## Ракета в коде\n```\n"
        # Длинный забор с ``` внутри и забор тильдами.
        "\n````\n```\n## Ракета два\n```\n````\n"
        "\n~~~\n## Ракета три\n~~~\n"
        # Строка с кодом в строке — не забор: заголовок после неё остаётся заголовком.
        "\n```код``` в тексте\n## Ракета снаружи ##\n"
    )
    pvd.write_text(pvd.read_text(encoding="utf-8") + fenced, encoding="utf-8")
    service = Kd2Service(
        Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace", reference_dir=copy)
    )
    assert service.reference_get("pvd", key="ракета")["headings"] == ["Ракета снаружи"]


def test_rule_kind_level_one_heading_ends_section(tmp_path: Path) -> None:
    copy = tmp_path / "skills"
    shutil.copytree(SKILLS, copy)
    pvd = copy / "kd2-rules-build" / "references" / "pvd.md"
    pvd.write_text(
        pvd.read_text(encoding="utf-8") + "\n## Ракета\nтекст\n# Другой файл\nчужое\n", "utf-8"
    )
    service = Kd2Service(
        Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace", reference_dir=copy)
    )
    text = service.reference_get("pvd", key="ракета")["parts"][0]["text"]
    assert text == "## Ракета\nтекст"


# --- Ключ ---------------------------------------------------------------------------------


def test_key_protocol_code_from_range(service: Kd2Service) -> None:
    rows = _rows(service.reference_get("protocol", key="33"))
    assert len(rows) == 1 and rows[0].startswith("| 31–34 |")
    assert not any(row.startswith("| 13 |") for row in rows)


def test_key_protocol_code_is_not_a_part_of_number(service: Kd2Service) -> None:
    rows = _rows(service.reference_get("protocol", key="17"))
    assert [row.split("|")[1].strip() for row in rows] == ["16, 17"]
    assert _rows(service.reference_get("protocol", key="174"))[0].startswith("| 174 |")


def test_key_codes_parse_lists_and_ranges() -> None:
    assert codes("19, 20, 21") == {19, 20, 21}
    assert codes("31–34") == {31, 32, 33, 34}
    assert codes("22, 23, 62, 63, 78") == {22, 23, 62, 63, 78}


def test_key_protocol_code_with_words(service: Kd2Service) -> None:
    for key in ("код 33", "33,", " 33 "):
        assert _rows(service.reference_get("protocol", key=key))[0].startswith("| 31–34 |")
    assert _rows(service.reference_get("protocol", key="²")) == []  # не int(), не ошибка


def test_key_event_in_phases(service: Kd2Service) -> None:
    result = service.reference_get("phases", key="ПослеЗагрузки")
    assert result["tables"][0]["header"].startswith("| Обработчик |")
    first_cells = [row.split("|")[1].strip() for row in _rows(result)]
    assert first_cells == ["`ПослеЗагрузки`"]  # не ПослеЗагрузкиДанных и не …Объекта


def test_key_event_of_pvd_and_kind(service: Kd2Service) -> None:
    rows = _rows(service.reference_get("phases", key="ПередОбработкойПравила"))
    assert len(rows) == 1 and "стр. 18173" in rows[0]
    kinds = {row.split("|")[2].strip() for row in _rows(service.reference_get("phases", key="ПКО"))}
    assert kinds == {"ПКО"}


def test_key_symptom_in_words(service: Kd2Service) -> None:
    rows = _rows(service.reference_get("symptoms", key="задвоились"))
    assert any("Задвоились контрагенты" in row for row in rows)
    assert _rows(service.reference_get("symptoms", key="НЕПРОВЕДЕННЫМ"))  # ё и регистр
    assert _rows(service.reference_get("symptoms", key="задвоение")) == rows  # основа
    assert _rows(service.reference_get("symptoms", key="строки пропали две группы"))  # порядок


def test_key_not_found(service: Kd2Service) -> None:
    result = service.reference_get("protocol", key="999")
    assert result["tables"] == [] and result["total"] == 0
    assert "reference_search" in result["hint"]


def test_key_rows_are_paged(service: Kd2Service) -> None:
    whole = service.reference_get("phases", key="ПКО", limit=200)
    page = service.reference_get("phases", key="ПКО", offset=1, limit=2)
    assert page["total"] == whole["total"] > 3 and page["has_more"] is True
    assert _rows(page) == _rows(whole)[1:3]


# --- Источник один, ссылки на БСП ---------------------------------------------------------


def _section_table_rows() -> list[tuple[str, int, str]]:
    """Строки таблиц разделов с ключом по таблицам; разделы по заголовкам — со своими правилами."""
    found = []
    for section in SECTIONS:
        if section.key_headings:
            continue
        rows = numbered(read_lines(SKILLS, section), section)
        found += [(section.file, line, text) for _, line, text in table_rows(rows)]
    return found


def test_bsp_refs_in_every_table_row() -> None:
    rows = _section_table_rows()
    assert len(rows) > 80
    missing = [f"{file}:{line}" for file, line, text in rows if not BSP_REF.search(text)]
    assert missing == [], "строки таблиц справки без ссылки на строку БСП"


def test_source_text_is_not_in_code() -> None:
    code = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py"))
    copied = [
        f"{file}:{line}"
        for file, line, text in _section_table_rows()
        for cell in text.split("|")
        if len(cell.strip()) > 60 and cell.strip() in code
    ]
    assert copied == [], "текст справки скопирован в код сервера"


def test_section_text_is_not_in_code() -> None:
    """Источник один: ни одна строка любого раздела (длиннее 60 знаков) не повторена в коде."""
    code = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py"))
    copied = [
        f"{section.file}:{line}"
        for section in SECTIONS
        for line, text in numbered(read_lines(SKILLS, section), section)
        if len(text.strip()) > 60 and text.strip() in code
    ]
    assert copied == [], "текст справки скопирован в код сервера"


def test_source_headings_exist() -> None:
    for section in SECTIONS:
        lines = read_lines(SKILLS, section)
        try:
            spans(lines, section)
        except KeyError as error:
            pytest.fail(f"{section.id}: нет заголовка «{error.args[0]}» в {section.file}")


def test_source_renamed_heading_is_reported(tmp_path: Path) -> None:
    copy = tmp_path / "skills"
    shutil.copytree(SKILLS, copy)
    handlers = copy / "kd2-rules-build" / "references" / "handlers.md"
    text = handlers.read_text(encoding="utf-8")
    handlers.write_text(text.replace("## Где и когда вызывается", "## Где вызывается"), "utf-8")
    phases = next(section for section in SECTIONS if section.id == "phases")
    with pytest.raises(KeyError, match="Где и когда вызывается"):
        spans(read_lines(copy, phases), phases)
    service = Kd2Service(
        Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace", reference_dir=copy)
    )
    with pytest.raises(ReferenceUnavailableError, match="Где и когда вызывается"):
        service.reference_get()
    (copy / "kd2-exchange-pitfalls" / "references" / "protocol.md").unlink()
    with pytest.raises(ReferenceUnavailableError, match=r"protocol\.md"):
        service.reference_get("protocol")


def test_skill_name_rule_matches_scripts() -> None:
    assert SKILL_NAME_RE.pattern == our_skills.SKILL_NAME + r"\Z"


def test_unavailable(tmp_path: Path) -> None:
    service = Kd2Service(
        Settings(
            cache_dir=tmp_path / "cache",
            workspace=tmp_path / "workspace",
            reference_dir=tmp_path / "nothing",
        )
    )
    with pytest.raises(ReferenceUnavailableError):
        service.reference_get()
    with pytest.raises(ReferenceUnavailableError):
        service.reference_search("отказ")


def test_reference_dir_from_env() -> None:
    settings = Settings.from_env({"KD2_REFERENCE_DIR": str(SKILLS)})
    assert settings.reference_dir == SKILLS
    assert resolve_root(settings.reference_dir) == SKILLS
    assert Settings.from_env({}).reference_dir is None


# --- Поиск --------------------------------------------------------------------------------


def test_search_hit_with_place(service: Kd2Service) -> None:
    result = service.reference_search("ПоследовательностьПолейПоиска", limit=50)
    files = {item["file"] for item in result["items"]}
    assert "kd2-rules-build/references/search.md" in files
    assert "kd2-rules-build/references/handlers.md" in files
    item = result["items"][0]
    assert item["line"] > 0 and item["heading"]
    assert "[ПоследовательностьПолейПоиска]" in item["snippet"]


def test_search_identifier_with_dot_and_underscore(service: Kd2Service) -> None:
    result = service.reference_search("search.continue_without_fields")
    assert result["complete"] == result["total"] >= 2
    assert all("[search.continue_without_fields]" in s for s in _hits(result))


def test_search_query_syntax_is_text(service: Kd2Service) -> None:
    for query in ('"Отказ" AND (ПКО*', 'NEAR(" ")', "a:b:c", "kd2-rules-build"):
        result = service.reference_search(query)
        assert result["offset"] == 0  # ответ, а не ошибка разбора FTS
    assert service.reference_search("kd2-rules-build")["terms"] == ['"kd2 rules build"']
    for query in ("***", "-", '"', ":"):
        with pytest.raises(Kd2Error, match="нет слов"):
            service.reference_search(query)


def test_search_identifier_1c_is_exact(service: Kd2Service) -> None:
    result = service.reference_search("ПослеЗагрузки", limit=50)
    assert result["terms"] == ['"послезагрузки"']
    for item in result["items"]:  # слово — в тексте блока или в его заголовке
        assert "[ПослеЗагрузки]" in item["snippet"] or "ПослеЗагрузки`" in item["heading"]
        assert "ПослеЗагрузкиОбъекта]" not in item["snippet"]


def test_search_stop_words_and_partial_matches(service: Kd2Service) -> None:
    result = service.reference_search("объект не регистрируется для узла", limit=50)
    assert result["ignored_terms"] == ["не", "для"]
    matched = [item["matched"] for item in result["items"]]
    assert matched == sorted(matched, reverse=True)
    assert result["complete"] == matched.count(2)
    places = [(i["file"], i["line"]) for i in result["items"]]
    assert ("kd2-exchange-pitfalls/references/symptoms.md", 17) in places
    lone = service.reference_search("не для")  # только служебные — ищутся ими
    assert "ignored_terms" not in lone and lone["total"] > 0


def test_search_endings_and_lookalikes() -> None:
    assert terms("табличных ошибок данных")[0][0].fts == '"табличн"*'
    assert [t.fts for t in terms("табличных ошибок данных")[0]] == [
        '"табличн"*',
        '"ошиб"*',
        '"данн"*',
    ]
    assert terms("пкo")[0][0].fts == '"пко"*'  # латинская «o»
    assert stem("регистрация") == "регистраци"  # не смешивать с регистрами


def test_search_heading_counts(service: Kd2Service) -> None:
    result = service.reference_search("исключение обработчике транзакция откатывается")
    assert result["complete"] >= 1
    assert result["items"][0]["file"] == "kd2-rules-build/references/handlers.md"


def test_search_skips_json_examples() -> None:
    lines = [
        "# Т",
        "",
        "```json",
        '{"snippet":"зюзюкалка"}',
        "```",
        "",
        "```bsl",
        "Зюзюкалка = 1;",
        "```",
    ]
    found = list(blocks("f.md", lines))
    assert [b.line for b in found] == [1, 7]
    assert "Зюзюкалка = 1;" in found[1].text


def test_search_word_forms(service: Kd2Service) -> None:
    assert any(
        "[Задвоились]" in s for s in _hits(service.reference_search("задвоение контрагента"))
    )
    assert any("непровед" in s.lower() for s in _hits(service.reference_search("непроведенный")))


def test_search_missing_word_keeps_partial_matches(service: Kd2Service) -> None:
    result = service.reference_search("контрагент абракадабра")
    assert result["complete"] == 0
    assert {item["matched"] for item in result["items"]} == {1}
    assert result["missing_terms"] == ["абракадабра"]
    assert result["total"] > 0
    assert "missing_terms" not in service.reference_search("контрагент")


def test_search_is_deterministic(service: Kd2Service, tmp_path: Path) -> None:
    other = Kd2Service(Settings(cache_dir=tmp_path / "c2", workspace=tmp_path / "w2"))
    query = "отказ объект не записывается"
    assert service.reference_search(query, limit=50) == other.reference_search(query, limit=50)


def test_search_pages_and_limits(service: Kd2Service) -> None:
    first = service.reference_search("отказ", limit=5)
    second = service.reference_search("отказ", offset=5, limit=5)
    assert first["has_more"] and first["limit"] == 5
    assert first["items"] != second["items"]
    assert service.reference_search("отказ", limit=500)["limit"] == 50
    with pytest.raises(Kd2Error):
        service.reference_search("   ")
    with pytest.raises(Kd2Error):
        service.reference_search("я" * 257)


def test_search_index_follows_file_changes(tmp_path: Path) -> None:
    copy = tmp_path / "skills"
    shutil.copytree(SKILLS, copy)
    service = Kd2Service(
        Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace", reference_dir=copy)
    )
    assert service.reference_search("зюзюкалка")["total"] == 0
    note = copy / "kd2-exchange-pitfalls" / "references" / "new.md"
    note.write_text("# Новое\n\nЗюзюкалка ломает обмен.\n", encoding="utf-8")
    hit = service.reference_search("зюзюкалка")["items"][0]
    assert hit["file"] == "kd2-exchange-pitfalls/references/new.md" and hit["line"] == 3


def test_search_corpus_is_server_skills_only(tmp_path: Path) -> None:
    copy = tmp_path / "skills"
    shutil.copytree(SKILLS, copy)
    alien = copy / "other-skill"
    alien.mkdir(exist_ok=True)
    (alien / "SKILL.md").write_text("Зюзюкалка чужого скилла\n", encoding="utf-8")
    service = Kd2Service(
        Settings(cache_dir=tmp_path / "cache", workspace=tmp_path / "workspace", reference_dir=copy)
    )
    assert service.reference_search("зюзюкалка")["total"] == 0


def test_stem_and_terms() -> None:
    assert stem("задвоение") == "задво"
    assert stem("регистрируется") == "регистр"
    assert stem("пко") == "пко"
    found, ignored = terms("Задвоение ПКО 26 rules_validate AND и")
    assert {term.word: term.fts for term in found} == {
        "Задвоение": '"задво"*',
        "ПКО": '"пко"*',
        "26": '"26"',
        "rules_validate": '"rules validate"',
        "AND": '"and"*',
    }
    assert ignored == ["и"]


def test_search_skill_filter(service: Kd2Service) -> None:
    whole = service.reference_search("отказ", limit=50)
    kd2 = service.reference_search("отказ", skill="kd2", limit=50)
    assert kd2["skill"] == "kd2" and 0 < kd2["total"] < whole["total"]
    assert all(item["file"].startswith("kd2") for item in kd2["items"])
    assert service.reference_search("отказ", skill="kd9")["missing_terms"] == ["отказ"]
