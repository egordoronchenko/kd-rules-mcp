"""Маленькая структура метаданных прямо в SQLite (схема `structures/db.py`) для тестов."""

from collections.abc import Sequence
from pathlib import Path

from kd2_rules_mcp.structures import db

# (вид, имя, синоним, типы, вложенные свойства)
Prop = tuple[str, str, str, str, Sequence["Prop"]]


class StructureBuilder:
    """Маленькая структура прямо в SQLite (схема `structures/db.py`)."""

    def __init__(self, path: Path) -> None:
        self.conn = db.create(path)

    def add(
        self,
        kind: str,
        name: str,
        props: Sequence[Prop] = (),
        *,
        synonym: str = "",
        values: Sequence[tuple[str, str]] = (),
    ) -> None:
        prefix = {"Документ": "ДокументСсылка", "Справочник": "СправочникСсылка"}.get(kind, kind)
        cur = self.conn.execute(
            "INSERT INTO objects (kind, name, type_name, synonym) VALUES (?, ?, ?, ?)",
            (kind, name, f"{prefix}.{name}", synonym),
        )
        object_id = int(cur.lastrowid or 0)
        self._props(object_id, None, "", props)
        for value, value_synonym in values:
            self.conn.execute(
                "INSERT INTO object_values (object_id, name, synonym) VALUES (?, ?, ?)",
                (object_id, value, value_synonym),
            )

    def _props(
        self, object_id: int, parent: int | None, prefix: str, props: Sequence[Prop]
    ) -> None:
        for kind, name, synonym, types, children in props:
            type_set = None
            if types:
                self.conn.execute("INSERT OR IGNORE INTO type_sets (types) VALUES (?)", (types,))
                type_set = self.conn.execute(
                    "SELECT id FROM type_sets WHERE types = ?", (types,)
                ).fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO properties (object_id, parent_id, kind, name, path, synonym, "
                "type_set_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (object_id, parent, kind, name, f"{prefix}{name}", synonym, type_set),
            )
            self._props(object_id, int(cur.lastrowid or 0), f"{prefix}{name}.", children)
