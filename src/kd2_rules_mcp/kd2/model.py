"""Модель правил КД 2: дерево узлов, типизированных по схеме (`schema.py`).

Узел хранит значения по именам тегов и атрибутов 1С — так же их называет агент в инструментах.
Содержимое, которого нет в схеме, сохраняется как есть и выводится обратно (в конце узла).
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

from lxml import etree

from kd2_rules_mcp.kd2.schema import Kind, Scalar, kind
from kd2_rules_mcp.kd2.xmlstyle import XmlStyle


def rule_code(value: object) -> str:
    """Код или имя правила без хвостовых пробелов.

    КД хранит код строкой фиксированной длины и в выгрузке из базы дополняет его
    пробелами (до 50). Читатель обрезает хвост (`СокрП`, БСП:4495, БСП:4408).
    Сравнение ссылок, ключи и адреса правил используют только этот вид.
    """
    return str(value).rstrip()


@dataclass(eq=False, slots=True)
class Node:
    """Элемент правил: атрибуты, простые значения, вложенные элементы и список правил/групп."""

    kind: Kind
    tag: str
    attrs: dict[str, Scalar] = field(default_factory=dict)
    values: dict[str, Scalar] = field(default_factory=dict)
    children: dict[str, "Node"] = field(default_factory=dict)
    items: list["Node"] = field(default_factory=list)
    text: str | None = None
    # Элементы, которых нет в схеме вида, — в исходном виде.
    unknown: list[etree._Element] = field(default_factory=list)
    # Текст между элементами (маркеры доработок `//bt_N` … `//bt_K`): перед этим узлом
    # в родителе и после последнего дочернего элемента этого узла.
    leading_text: str | None = None
    trailing_text: str | None = None

    def __repr__(self) -> str:
        code = self.code
        return f"Node({self.kind.title}: {self.tag}{f'[{code}]' if code else ''})"

    @classmethod
    def new(cls, kind_name: str, tag: str) -> "Node":
        """Новый узел вида `kind_name` с атрибутами, которые писатель КД выводит всегда."""
        node_kind = kind(kind_name)
        attrs = {a.name: a.default for a in node_kind.attrs if a.default is not None}
        return cls(node_kind, tag, attrs=attrs)

    def get(self, tag: str, default: Scalar = "") -> Scalar:
        """Простое значение дочернего тега."""
        return self.values.get(tag, default)

    def child(self, tag: str) -> "Node | None":
        """Вложенный элемент по тегу."""
        return self.children.get(tag)

    @property
    def code(self) -> str:
        """Код правила (`Код`) или имя (`Имя`) для алгоритмов и запросов."""
        value = self.values.get("Код", self.attrs.get("Имя", ""))
        return rule_code(value)

    @property
    def is_group(self) -> bool:
        """Узел — группа правил."""
        return self.tag == "Группа"

    def is_empty(self) -> bool:
        """В узле нет ни атрибутов, ни содержимого."""
        return not (
            self.attrs
            or self.values
            or self.children
            or self.items
            or self.text
            or self.unknown
            or self.trailing_text
        )

    def walk(self) -> Iterator["Node"]:
        """Все правила списка с раскрытием групп (сами группы не возвращаются)."""
        for item in self.items:
            if item.is_group:
                yield from item.walk()
            else:
                yield item

    def walk_all(self) -> Iterator["Node"]:
        """Все узлы поддерева, включая этот, в порядке документа."""
        yield self
        for node in self.children.values():
            yield from node.walk_all()
        for item in self.items:
            yield from item.walk_all()


@dataclass(eq=False, slots=True)
class RulesDocument:
    """Файл правил: корневой узел и стиль исходного XML."""

    root: Node
    style: XmlStyle
    # Байты файла, из которого разобран документ. Сериализатор возвращает им
    # исходные концы строк неизменённым строкам. У собранного заново документа пусто.
    origin: bytes | None = None

    @property
    def root_tag(self) -> str:
        """Корневой тег: `ПравилаОбмена` или `ПравилаРегистрации`."""
        return self.root.tag

    def section(self, tag: str) -> Node:
        """Раздел верхнего уровня (контейнер списка правил); создаётся пустым, если его нет."""
        node = self.root.children.get(tag)
        if node is None:
            node = Node(kind(self.root.kind.children[tag].kind), tag)
            self.root.children[tag] = node
        return node


class ExchangeRules(RulesDocument):
    """Правила обмена «ПравилаОбмена» 2.01."""

    __slots__ = ()

    def config(self, tag: str) -> tuple[str, dict[str, Scalar]]:
        """Имя и атрибуты конфигурации `Источник` или `Приемник`."""
        node = self.root.child(tag)
        if node is None:
            return "", {}
        return node.text or "", dict(node.attrs)

    @property
    def source_name(self) -> str:
        """Имя конфигурации-источника."""
        return self.config("Источник")[0]

    @property
    def target_name(self) -> str:
        """Имя конфигурации-приёмника."""
        return self.config("Приемник")[0]

    def pko(self) -> list[Node]:
        """Все ПКО с раскрытием групп."""
        return list(self.section("ПравилаКонвертацииОбъектов").walk())

    def pvd(self) -> list[Node]:
        """Все ПВД с раскрытием групп."""
        return list(self.section("ПравилаВыгрузкиДанных").walk())

    def pod(self) -> list[Node]:
        """Все ПОД с раскрытием групп."""
        return list(self.section("ПравилаОчисткиДанных").walk())

    def algorithms(self) -> list[Node]:
        """Все алгоритмы с раскрытием групп."""
        return list(self.section("Алгоритмы").walk())

    def queries(self) -> list[Node]:
        """Все запросы с раскрытием групп."""
        return list(self.section("Запросы").walk())


class RegistrationRules(RulesDocument):
    """Правила регистрации «ПравилаРегистрации»."""

    __slots__ = ()

    @property
    def exchange_plan(self) -> str:
        """Имя плана обмена (атрибут `Имя` тега `ПланОбмена`)."""
        node = self.root.child("ПланОбмена")
        return str(node.attrs.get("Имя", "")) if node is not None else ""

    def plan_content(self) -> list[Node]:
        """Элементы состава плана обмена."""
        return list(self.section("СоставПланаОбмена").items)

    def rules(self) -> list[Node]:
        """Все правила регистрации объектов с раскрытием групп."""
        return list(self.section("ПравилаРегистрацииОбъектов").walk())
