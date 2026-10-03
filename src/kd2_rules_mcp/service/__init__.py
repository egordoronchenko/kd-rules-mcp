"""Логика инструментов MCP без транспорта (спецификация `mcp-service`).

Каждый публичный метод `Kd2Service` — один инструмент: принимает простые значения, возвращает
словарь, пригодный для JSON, и бросает `Kd2Error` при отказе.
Ответы компактные: списки — постранично (`offset`, `limit`, `has_more`), XML правил целиком
не возвращается, тексты обработчиков обрезаются.

Пути. Агент передаёт пути так, как видит их на своей машине (`D:\\Repos\\bp\\…`); в контейнере
исходники смонтированы в другое место. `PathMap` переводит путь агента в локальный и обратно
(переменная `KD2_PATH_MAP`: `D:\\Repos\\bp=/projects/bp;…`). Пишется только в рабочую папку.
"""

from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.checks import ChecksMixin
from kd2_rules_mcp.service.ed import EdMixin
from kd2_rules_mcp.service.ed_schema import EdSchemaMixin
from kd2_rules_mcp.service.generate import GenerateMixin
from kd2_rules_mcp.service.matching import MatchingMixin
from kd2_rules_mcp.service.paths import PathMap, Settings
from kd2_rules_mcp.service.rules import RulesMixin
from kd2_rules_mcp.service.structures import StructuresMixin

__all__ = ["Kd2Service", "PathMap", "Settings"]


class Kd2Service(
    EdSchemaMixin,
    EdMixin,
    StructuresMixin,
    MatchingMixin,
    RulesMixin,
    ChecksMixin,
    GenerateMixin,
    ServiceBase,
):
    """Состояние сервера: кэш структур, рабочая папка с открытыми проектами правил."""
