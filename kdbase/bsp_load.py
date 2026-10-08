"""Загрузка правил БСП через сервер данных базы: код 1С, вызов и разбор протокола.

Комплект с записью в регистр — `load_rules_code(..., write=True)` (`exchange_check`).
Проверка без `Записать` — `write=False` (`bsp_check`, транспорт сервера данных).
"""

import base64
import http.client
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from kd_rules_mcp.projects import data_endpoint, load_catalog, load_local

ROOT = Path(__file__).resolve().parents[1]
TOOL = "vcexecutecode"
TIMEOUT_S = 900


class ExchangeCheckError(Exception):
    """Ошибка шага проверки: текст — для протокола."""


def bsl(value: str) -> str:
    """Строковый литерал 1С."""
    return '"' + value.replace('"', '""') + '"'


def one_line(code: str) -> str:
    """Код одной строкой: многострочный код сервер данных не выполняет (ответ пустой)."""
    return " ".join(line.strip() for line in code.splitlines() if line.strip())


def guarded(body: str) -> str:
    """Тело в `Попытка`: результат «OK …» или «ОШИБКА <подробное представление>»."""
    return one_line(
        f"""Попытка
        {body}
        Исключение
        Результат = "ОШИБКА " + ПодробноеПредставлениеОшибки(ИнформацияОбОшибке());
        КонецПопытки;"""
    )


def load_rules_code(
    plan: str,
    archive: bytes,
    file_name: str,
    *,
    write: bool,
    full_set: bool = True,
) -> str:
    """Одна строка кода 1С для `DataServer.call`.

    full_set=True — `ЗагрузитьКомплектПравил` (две записи: конвертация и регистрация).
    full_set=False — `ЗагрузитьПравила(..., Истина)` (одна запись конвертации, ветка «это архив»).
    write=True — только вместе с full_set=True: обе записи `.Записать()`, успех —
    `Результат = "OK"`.
    write=False — `.Записать()` нет; `Результат` — протокол ниже.
    """
    if write and not full_set:
        raise ValueError("write=True допустим только при full_set=True")
    payload = base64.b64encode(archive).decode("ascii")
    if write:
        return guarded(_write_rules_body(plan, payload, file_name))
    return guarded(_report_rules_body(plan, payload, file_name, full_set))


def _write_rules_body(plan: str, payload: str, file_name: str) -> str:
    """Комплект в регистр, как форма загрузки: обе записи `.Записать()`, успех — «OK»."""
    kinds = "ПравилаКонвертацииОбъектов,ПравилаРегистрацииОбъектов"
    return f"""Рег = РегистрыСведений.ПравилаДляОбменаДанными; Записи = Новый Массив;
        Для Каждого Вид Из СтрРазделить({bsl(kinds)}, ",") Цикл
        З = Рег.СоздатьМенеджерЗаписи(); З.ИмяПланаОбмена = {bsl(plan)};
        З.ВидПравил = Перечисления.ВидыПравилДляОбменаДанными[Вид];
        З.ИсточникПравил = Перечисления.ИсточникиПравилДляОбменаДанными.Файл;
        Записи.Добавить(З);
        КонецЦикла;
        Данные = Новый Структура("ЗаписьПравилКонвертации, ЗаписьПравилРегистрации",
        Записи[0], Записи[1]);
        Адрес = ПоместитьВоВременноеХранилище(Base64Значение({bsl(payload)}));
        Отказ = Ложь; Описание = "";
        Рег.ЗагрузитьКомплектПравил(Отказ, Данные, Описание, Адрес, {bsl(file_name)});
        Если Отказ Или Не Записи[0].ПравилаЗагружены Или Не Записи[1].ПравилаЗагружены Тогда
        Тексты = Новый Массив; Тексты.Добавить(Описание);
        Для Каждого С Из ПолучитьСообщенияПользователю(Истина) Цикл
        Тексты.Добавить(С.Текст);
        КонецЦикла;
        ВызватьИсключение "Правила не загружены: " + СтрСоединить(Тексты, "; ");
        КонецЕсли;
        Записи[0].Записать(); Записи[1].Записать();
        Результат = "OK";"""


def _report_rules_body(plan: str, payload: str, file_name: str, full_set: bool) -> str:
    """Те же менеджеры и вызов, без `.Записать()`; `Результат` — протокол с длинами полей.

    `ЧН=0` рядом с `ЧГ=`: `Формат(0, "ЧГ=")` в 1С — пустая строка, а нулевая длина
    и ноль сообщений в протоколе должны быть цифрой.
    """
    kinds = "ПравилаКонвертацииОбъектов"
    if full_set:
        kinds = f"{kinds},ПравилаРегистрацииОбъектов"
        call = f"""Данные = Новый Структура("ЗаписьПравилКонвертации, ЗаписьПравилРегистрации",
        Записи[0], Записи[1]);
        Отказ = Ложь; Описание = "";
        Рег.ЗагрузитьКомплектПравил(Отказ, Данные, Описание, Адрес, {bsl(file_name)});"""
    else:
        call = f"""Отказ = Ложь;
        Рег.ЗагрузитьПравила(Отказ, Записи[0], Адрес, {bsl(file_name)}, Истина);"""
    return f"""Рег = РегистрыСведений.ПравилаДляОбменаДанными; Записи = Новый Массив;
        Для Каждого Вид Из СтрРазделить({bsl(kinds)}, ",") Цикл
        З = Рег.СоздатьМенеджерЗаписи(); З.ИмяПланаОбмена = {bsl(plan)};
        З.ВидПравил = Перечисления.ВидыПравилДляОбменаДанными[Вид];
        З.ИсточникПравил = Перечисления.ИсточникиПравилДляОбменаДанными.Файл;
        Записи.Добавить(З);
        КонецЦикла;
        Адрес = ПоместитьВоВременноеХранилище(Base64Значение({bsl(payload)}));
        {call}
        Части = Новый Массив;
        Части.Добавить("OK");
        Части.Добавить("R");
        Части.Добавить(Формат(Записи.Количество(), "ЧН=0; ЧГ="));
        Для Каждого З Из Записи Цикл
        Части.Добавить(?(З.ПравилаЗагружены, "1", "0"));
        Текст = ?(З.ИнформацияОПравилах = Неопределено, "", Строка(З.ИнформацияОПравилах));
        Части.Добавить(Формат(СтрДлина(Текст), "ЧН=0; ЧГ="));
        Части.Добавить(Текст);
        КонецЦикла;
        Части.Добавить("M");
        Сообщения = ПолучитьСообщенияПользователю(Истина);
        Части.Добавить(Формат(Сообщения.Количество(), "ЧН=0; ЧГ="));
        Для Каждого С Из Сообщения Цикл
        Текст = Строка(С.Текст);
        Части.Добавить(Формат(СтрДлина(Текст), "ЧН=0; ЧГ="));
        Части.Добавить(Текст);
        КонецЦикла;
        Результат = СтрСоединить(Части, Символы.ПС);"""


@dataclass(frozen=True, slots=True)
class LoadReport:
    """Протокол ветки `write=False`: флаги записей, сведения и сообщения пользователю."""

    flags: tuple[bool, ...]
    infos: tuple[str, ...]
    messages: tuple[str, ...]


def parse_load_report(text: str) -> LoadReport:
    """`Результат` ветки write=False, целиком, с префиксом `OK`.

    LoadReport — поля `flags: tuple[bool, ...]`, `infos: tuple[str, ...]`,
    `messages: tuple[str, ...]`. Длины `flags` и `infos` совпадают.
    Строка без префикса `OK`, обрыв длины или лишний хвост — `ValueError`.
    """
    if not text.startswith("OK"):
        raise ValueError("протокол без префикса OK")
    pos = 0
    header, pos = _take_line(text, pos)
    if header != "OK":
        raise ValueError("протокол без префикса OK")
    marker, pos = _take_line(text, pos)
    if marker != "R":
        raise ValueError("обрыв протокола")
    count, pos = _take_count(text, pos)
    flags: list[bool] = []
    infos: list[str] = []
    for _ in range(count):
        flag, pos = _take_line(text, pos)
        if flag not in ("0", "1"):
            raise ValueError("обрыв протокола")
        length, pos = _take_count(text, pos)
        info, pos = _take_field(text, pos, length)
        flags.append(flag == "1")
        infos.append(info)
    marker, pos = _take_line(text, pos)
    if marker != "M":
        raise ValueError("обрыв протокола")
    count, pos = _take_count(text, pos)
    messages: list[str] = []
    for _ in range(count):
        length, pos = _take_count(text, pos)
        message, pos = _take_field(text, pos, length)
        messages.append(message)
    if pos != len(text):
        raise ValueError("лишний хвост")
    return LoadReport(tuple(flags), tuple(infos), tuple(messages))


def _take_line(text: str, pos: int) -> tuple[str, int]:
    """Строка заголовка до перевода; перевод съедается. Конца текста нет — `ValueError`."""
    if pos > len(text):
        raise ValueError("обрыв протокола")
    end = text.find("\n", pos)
    if end < 0:
        return text[pos:], len(text)
    return text[pos:end], end + 1


def _take_count(text: str, pos: int) -> tuple[int, int]:
    """Целое без группировки разрядов (`Формат(..., "ЧГ=")`)."""
    raw, pos = _take_line(text, pos)
    if not raw.isdecimal():
        raise ValueError("обрыв протокола")
    return int(raw), pos


def _take_field(text: str, pos: int, length: int) -> tuple[str, int]:
    """Ровно `length` символов поля и один перевод-разделитель склейки, если он ещё есть."""
    if length < 0 or pos + length > len(text):
        raise ValueError("обрыв длины")
    end = pos + length
    value = text[pos:end]
    if end < len(text) and text[end] == "\n":
        end += 1
    return value, end


def short_error(text: str) -> str:
    """Ошибка 1С без эха нашего кода и стека HTTP-сервиса.

    Текст до первой строки стека «{…}». Если в подробном представлении есть
    «по причине:», к краткому тексту добавляется первая содержательная строка
    после последнего «по причине:» — сама причина платформы.
    """
    text = re.sub(r"[A-Za-z0-9+/=]{200,}", "<base64>", text)
    body = text.removeprefix("ОШИБКА ")
    reason = ""
    marker = "по причине:"
    if marker in body:
        tail = body[body.rfind(marker) + len(marker) :]
        for line in tail.splitlines():
            stripped = line.strip()
            if stripped:
                reason = stripped
                break
    kept: list[str] = []
    for line in body.splitlines():
        if kept and line.lstrip().startswith("{"):
            break
        kept.append(line.strip())
    head = " ".join(part for part in kept if part)
    if reason and reason not in head:
        return f"{head} {reason}".strip()
    return head


@dataclass(frozen=True, slots=True)
class DataServer:
    """Сервер данных базы-песочницы: `vcexecutecode` по HTTP (JSON-RPC MCP)."""

    label: str
    url: str
    headers: dict[str, str]

    def call(self, code: str) -> str:
        """Текст ответа инструмента; ошибка HTTP или JSON-RPC — `ExchangeCheckError`."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": TOOL, "arguments": {"bslcode": code}},
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                **self.headers,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                raw = response.read().decode("utf-8")
        except (UnicodeDecodeError, http.client.HTTPException) as error:
            raise ExchangeCheckError(
                f"{self.label}: не-JSON ответ или обрыв HTTP: {error}"
            ) from error
        except OSError as error:
            raise ExchangeCheckError(f"{self.label}: сервер данных недоступен: {error}") from error
        try:
            answer = json.loads(_sse_data(raw))
        except json.JSONDecodeError as error:
            raise ExchangeCheckError(
                f"{self.label}: не-JSON ответ или обрыв HTTP: {error}"
            ) from error
        if "error" in answer:
            raise ExchangeCheckError(f"{self.label}: {answer['error']}")
        result = answer.get("result", {})
        return "".join(part.get("text", "") for part in result.get("content", []))

    def run(self, code: str) -> str:
        """Код из `guarded`: текст после «OK»; «ОШИБКА …» или пустой ответ — ошибка шага.

        Не-JSON ответ и обрыв HTTP (в том числе из переопределённого `call`) —
        транспортная ошибка: вызывающий может перечитать состояние, не повторяя запись.
        """
        try:
            text = self.call(code)
        except (json.JSONDecodeError, UnicodeDecodeError, http.client.HTTPException) as error:
            raise ExchangeCheckError(
                f"{self.label}: не-JSON ответ или обрыв HTTP: {error}"
            ) from error
        if text.startswith("OK"):
            return text[2:].removeprefix(" ").removeprefix("\n")
        if not text:
            raise ExchangeCheckError(
                f"{self.label}: пустой ответ сервера данных "
                f"(код не выполнился или нет инструмента {TOOL})"
            )
        raise ExchangeCheckError(f"{self.label}: {short_error(text)}")


def _sse_data(raw: str) -> str:
    """Тело JSON-RPC: ответ в формате SSE (`data: …`) или обычный JSON."""
    for line in raw.splitlines():
        if line.startswith("data:"):
            return line[5:].strip()
    return raw


def server_for(ref: str, root: Path = ROOT) -> DataServer:
    """Сервер данных базы `<проект>.<база>` из projects.yaml (только песочница)."""
    project_id, _, base_id = ref.partition(".")
    if not base_id:
        raise ExchangeCheckError(f"База задаётся как <проект>.<база>, получено «{ref}»")
    catalog = load_catalog(root / "projects.yaml")
    local = load_local(root / "projects.local.yaml")
    url, headers = data_endpoint(catalog, local, project_id, base_id)
    return DataServer(ref, url, headers)
