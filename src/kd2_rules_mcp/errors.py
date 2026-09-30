"""Типизированные ошибки сервера (сообщения — на русском)."""


class Kd2Error(Exception):
    """Базовая ошибка сервера правил КД 2."""


class RulesFormatError(Kd2Error):
    """Файл правил не разбирается или не соответствует формату КД 2."""


class StructureFormatError(Kd2Error):
    """Файл структуры метаданных не разбирается или не является ожидаемой выгрузкой."""


class StructureNotFoundError(Kd2Error):
    """Структуры с таким идентификатором нет в кэше."""


class WorkspacePathError(Kd2Error):
    """Путь сохранения лежит вне рабочей папки."""


class ProjectNotFoundError(Kd2Error):
    """Рабочего проекта с таким идентификатором нет."""


class RuleEditError(Kd2Error):
    """Правка правила отклонена."""


class UnknownFieldError(RuleEditError):
    """Поле не входит в схему этого вида правила."""


class DuplicateRuleError(RuleEditError):
    """Код или имя уже заняты в своём списке."""


class RuleNotFoundError(RuleEditError):
    """Правило по адресу не найдено."""


class DanglingReferenceError(RuleEditError):
    """Ссылка на отсутствующее правило, объект, свойство или значение."""
