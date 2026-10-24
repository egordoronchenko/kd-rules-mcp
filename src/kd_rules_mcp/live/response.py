"""Единый ответ платформы и безопасные строковые выражения 1С."""

import json
import re
from typing import Any

from kd_rules_mcp.errors import LiveCodeError, LiveEmptyError, LiveParseError


def literal(value: str) -> str:
    """Значение остаётся строкой, даже если содержит код, комментарии или переводы."""
    parts = re.split(r"([\x00-\x08\x0a-\x1f\x85\u2028\u2029])", value)
    pieces = []
    for part in parts:
        if len(part) == 1 and ((ord(part) < 32 and part != "\t") or part in "\x85\u2028\u2029"):
            pieces.append(f"Символ({ord(part)})")
        else:
            # Две косые тоже разбиваем: в итоговом коде нет маркера комментария.
            pieces.append(
                ' + "/" + "/" + '.join('"' + p.replace('"', '""') + '"' for p in part.split("//"))
            )
    return "(" + " + ".join(pieces) + ")"


def one_line(code: str) -> str:
    """Переносы разрешены только между операторами, значения кодирует literal."""
    return " ".join(
        line.strip(" \t") for line in re.split(r"\r\n|\r|\n", code) if line.strip(" \t")
    )


def guarded(body: str) -> str:
    """Общий перехват ошибки платформы; тело задаёт переменную Результат."""
    return one_line(f"""Попытка {body}
        Исключение
        Результат = "ОШИБКА " + ПодробноеПредставлениеОшибки(ИнформацияОбОшибке());
        КонецПопытки;""")


def json_code(body: str, value: str = "Данные") -> str:
    """Сериализация структуры без коллизий разделителей и переводов строк."""
    return guarded(
        body
        + f""" Запись = Новый ЗаписьJSON; Запись.УстановитьСтроку();
        ЗаписатьJSON(Запись, {value}); Результат = "OK " + Запись.Закрыть();"""
    )


def short_error(text: str) -> str:
    """Причина платформы без стека и длинных base64-строк."""
    text = re.sub(r"[A-Za-z0-9+/=]{200,}", "<base64>", text)
    body = text.removeprefix("ОШИБКА ")
    reason = ""
    marker = "по причине:"
    if marker in body:
        reason = next((s.strip() for s in body.rsplit(marker, 1)[1].splitlines() if s.strip()), "")
    kept: list[str] = []
    for line in body.splitlines():
        if kept and line.lstrip().startswith("{"):
            break
        kept.append(line.strip())
    head = " ".join(part for part in kept if part)
    return f"{head} {reason}".strip() if reason and reason not in head else head


def parse_response(text: str) -> dict[str, Any]:
    """Строгий объект JSON: пустая структура допустима для команды без данных."""
    if not text.strip():
        raise LiveEmptyError("пустой ответ сервера данных: код не выполнился")
    if text.startswith("ОШИБКА "):
        raise LiveCodeError(short_error(text))
    try:
        if not text.startswith("OK "):
            raise ValueError
        value = json.loads(text[3:])
        if not isinstance(value, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise LiveParseError(f"неверный ответ: {short_error(text[:160])}") from None
    return value


def compatibility_error(value: Any, accept_version_mismatch: bool) -> str | None:
    """Совместимость БСП: разрешается только подтверждённое несовпадение версии."""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("kind"), str)
        or not isinstance(value.get("text"), str)
    ):
        raise LiveParseError("неверное поле ответа: compatibility")
    if value["kind"] in ("УстаревшиеПравила", "УстаревшаяВерсияКонфигурации"):
        if accept_version_mismatch:
            return None
        return (
            f"{value['text']}; человек может разрешить продолжить: "
            "accept_version_mismatch=true (как «Продолжить» формы БСП)"
        )
    return f"несовместимые правила: {value['kind']}: {value['text']}"
