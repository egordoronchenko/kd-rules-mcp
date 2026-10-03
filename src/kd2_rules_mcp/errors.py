"""Типизированные ошибки сервера (сообщения — на русском)."""

from collections.abc import Sequence
from typing import Any


class Kd2Error(Exception):
    """Базовая ошибка сервера правил КД 2."""


class EdSchemaNotFoundError(Kd2Error):
    """Схема формата не открыта."""


class EdSchemaTypeNotFoundError(Kd2Error):
    """Тип отсутствует в открытой схеме."""


class EdSchemaReadError(Kd2Error):
    """Файл пакета XDTO недоступен."""


class EdSchemaFormatError(Kd2Error):
    """Повреждённый XML или неподдержанный формат пакета."""


class EdSchemaConflictError(Kd2Error):
    """Один QName имеет разные определения."""


class EdSchemaAmbiguousImportError(Kd2Error):
    """Несколько описаний пакетов имеют одинаковый URI импорта."""


class EdSchemaProfileMismatchError(Kd2Error):
    """Версия, описание пакета или расширение не согласованы с базовым пакетом."""


class EdSchemaResourceLimitError(Kd2Error):
    """Превышен лимит чтения или хранения схем."""


class EdReadError(Kd2Error):
    """Файл менеджера ED недоступен или имеет неподдержанную кодировку."""


class EdFormatError(Kd2Error):
    """Модуль не соответствует безопасно читаемому формату менеджера ED."""


class EdResourceLimitError(Kd2Error):
    """Менеджер ED превышает предел размера или числа строк."""


class EdRouteProfileNotFoundError(Kd2Error):
    """Неизвестный или вытесненный снимок маршрутов."""


class EdRouteReadError(Kd2Error):
    """Выгрузка маршрутов недоступна по пути или корень не прочитать."""


class EdRouteFormatError(Kd2Error):
    """Это не полная XML-выгрузка конфигурации или повреждён Configuration.xml."""


class EdRouteResourceLimitError(Kd2Error):
    """Превышен лимит чтения выгрузки или хранения снимков маршрутов."""


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

    def __init__(self, message: str, candidates: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.candidates = list(candidates)
        self.candidate_page: dict[str, Any] | None = None


class DanglingReferenceError(RuleEditError):
    """Ссылка на отсутствующее правило, объект, свойство или значение."""
