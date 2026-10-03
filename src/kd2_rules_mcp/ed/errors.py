"""Ошибки чтения, независимые от транспорта и ошибок КД 2."""


class EdReadError(Exception):
    """Исходный файл невозможно прочитать."""


class EdFormatError(Exception):
    """Нарушены границы BSL или отсутствуют признаки менеджера."""


class EdResourceLimitError(Exception):
    """Превышен установленный предел входного документа."""
