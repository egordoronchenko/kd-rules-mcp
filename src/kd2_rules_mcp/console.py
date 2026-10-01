"""Вывод скриптов в консоль Windows: UTF-8 вместо кодировки консоли.

Консоль cp1251 не кодирует русский текст и символы вроде «→»: `print` падает
с `UnicodeEncodeError`. Скрипты вызывают `utf8_stdout()` первой строкой `main()`.
"""

import io
import sys


def utf8_stdout() -> None:
    """Переключает `sys.stdout` и `sys.stderr` на UTF-8, некодируемое заменяет.

    Потоки читаются в момент вызова. `io.TextIOWrapper` (у него есть `reconfigure`)
    переключается; поток без него (`io.StringIO`, подмена в тестах) пропускается.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
