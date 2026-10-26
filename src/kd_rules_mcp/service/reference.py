"""Инструменты справки: `reference_get` и `reference_search` (спецификация `reference-help`)."""

import threading
from pathlib import Path
from typing import Any

from kd_rules_mcp.errors import Kd2Error, ReferenceUnavailableError
from kd_rules_mcp.reference import resolve_root
from kd_rules_mcp.reference.search import ReferenceIndex, terms
from kd_rules_mcp.reference.sections import (
    SECTIONS,
    SECTIONS_BY_ID,
    Section,
    file_headings,
    file_title,
    group_rows,
    heading_rows,
    numbered,
    outline,
    parts,
    read_lines,
    rows_by_key,
)
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.views import page_limit, slice_rows

MAX_QUERY = 256
GET_LIMIT = 200
SEARCH_LIMIT = 50
_INDEX_LOCK = threading.Lock()


def _section_rows(root: Path, section: Section) -> tuple[list[str], list[tuple[int, str]]]:
    """Строки файла и раздела; нет файла или заголовка — `reference_unavailable` с адресом."""
    try:
        lines = read_lines(root, section)
        return lines, numbered(lines, section)
    except FileNotFoundError:
        raise ReferenceUnavailableError(f"Нет файла справки {section.file} в {root}") from None
    except KeyError as error:
        raise ReferenceUnavailableError(
            f"В {section.file} нет заголовка «## {error.args[0]}» раздела {section.id}"
        ) from None


class ReferenceMixin(ServiceBase):
    _reference_index: ReferenceIndex | None = None

    def _reference_root(self) -> Path:
        return resolve_root(self.settings.reference_dir)

    def reference_get(
        self, section: str | None = None, key: str | None = None, offset: int = 0, limit: int = 60
    ) -> dict[str, Any]:
        """Список разделов, текст раздела по строкам или строки таблиц раздела по ключу."""
        root = self._reference_root()
        if not section:
            items = []
            for item in SECTIONS:
                lines, rows = _section_rows(root, item)
                items.append(
                    {
                        "section": item.id,
                        "title": file_title(lines),
                        "file": item.file,
                        "headings": list(item.headings) or file_headings(lines),
                        "lines": len(rows),
                    }
                )
            return {"items": items, "total": len(items)}
        found = SECTIONS_BY_ID.get(section)
        if found is None:
            raise Kd2Error(
                f"Неизвестный раздел «{section}»; допустимые: {', '.join(SECTIONS_BY_ID)}"
            )
        lines, rows = _section_rows(root, found)
        view: dict[str, Any] = {"section": found.id, "title": file_title(lines), "file": found.file}
        if found.see:
            view["see"] = found.see
        if key is not None and key.strip() and found.key_headings:
            titles, matched = heading_rows(lines, key)
            page = slice_rows(matched, offset, min(limit, GET_LIMIT))
            view |= {"key": key, "headings": titles, "parts": parts(page.pop("items")), **page}
            if not titles:
                # Промах: оглавление файла отдельным ключом, `headings` — всегда совпавшие.
                view["outline"] = outline(lines)
                view["hint"] = "no heading matches the key; see outline or try reference_search"
            return view
        if key is not None and key.strip():
            page = slice_rows(rows_by_key(rows, found, key), offset, min(limit, GET_LIMIT))
            view |= {"key": key, "tables": group_rows(page.pop("items")), **page}
            if not page["total"]:
                view["hint"] = "no table row matches the key; try reference_search"
            return view
        page = slice_rows(rows, offset, min(limit, GET_LIMIT))
        view |= {"parts": parts(page.pop("items")), **page}
        return view

    def reference_search(
        self, query: str, skill: str | None = None, offset: int = 0, limit: int = 10
    ) -> dict[str, Any]:
        """Поиск по справочникам скиллов сервера: все слова в блоке, иначе по числу совпавших."""
        if not query.strip():
            raise Kd2Error("Пустая строка поиска")
        if len(query) > MAX_QUERY:
            raise Kd2Error(f"Длина строки поиска ограничена {MAX_QUERY} символами")
        if not terms(query)[0]:
            raise Kd2Error("В строке поиска нет слов: только знаки и символы")
        root = self._reference_root()
        with _INDEX_LOCK:
            index = self._reference_index
            if index is None:
                index = self._reference_index = ReferenceIndex()
        if offset < 0:
            raise Kd2Error(f"Смещение страницы не может быть отрицательным: {offset}")
        limit = min(page_limit(limit), SEARCH_LIMIT)
        result = index.search(root, query, offset, limit, skill or None)
        view = {"query": query, **({"skill": skill} if skill else {}), **result}
        view |= {"offset": offset, "limit": limit}
        view["has_more"] = offset + len(result["items"]) < result["total"]
        for name in ("missing_terms", "ignored_terms"):
            if not view[name]:
                del view[name]
        return view
