"""Типизированные ошибки сервера (сообщения — на русском)."""

from collections.abc import Sequence


class Kd2Error(Exception):
    """Базовая ошибка сервера правил КД 2."""


class RulesFormatError(Kd2Error):
    """Файл правил не разбирается или не соответствует формату КД 2."""


class StructureFormatError(Kd2Error):
    """Файл структуры метаданных не разбирается или не является ожидаемой выгрузкой."""


class StructureNotFoundError(Kd2Error):
    """Структуры с таким идентификатором нет в кэше."""


class ObjectNotFoundError(Kd2Error):
    """Объекта метаданных нет в структуре; `suggestions` — похожие имена."""

    def __init__(self, message: str, suggestions: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.suggestions = list(suggestions)


class WorkspacePathError(Kd2Error):
    """Путь записи лежит вне рабочей папки и папок живых правил проектов (rules_dir)."""


class ProjectNotFoundError(Kd2Error):
    """Рабочего проекта с таким идентификатором нет."""


class DuplicateProjectError(Kd2Error):
    """Идентификатор рабочего проекта уже занят."""


class RuleEditError(Kd2Error):
    """Правка правила отклонена."""


class UnknownFieldError(RuleEditError):
    """Поле не входит в схему этого вида правила."""


class DuplicateRuleError(RuleEditError):
    """Код или имя уже заняты в своём списке."""


class RuleNotFoundError(RuleEditError):
    """Правило по адресу не найдено."""


class AmbiguousAddressError(RuleNotFoundError):
    """Адрес ПКС совпал с несколькими правилами одного контейнера."""


class DanglingReferenceError(RuleEditError):
    """Ссылка на отсутствующее правило, объект, свойство или значение."""
