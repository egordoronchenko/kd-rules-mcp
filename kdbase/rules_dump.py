"""Снять исходные XML действующих правил БСП из песочницы, не обновляя их из макетов.

`--chunk-size` — число символов base64 в ответе (4..96000, кратно 4; по умолчанию 48000).
Каждая часть читается отдельным вызовом без зависимости от серверного сеанса.
SHA256 защищает от смены правил между вызовами и обрыва ответа сервера.
Корреспондент разделяет источник, имя файла, флаг и сведения записи конвертации.
"""

import argparse
import base64
import hashlib
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from bsp_load import DataServer, ExchangeCheckError, guarded, server_for, short_error
from live_checks import (
    json_code,
    literal,
    read_json,
    rule_description,
    rule_info_body,
    rule_record_body,
    stage,
    text_line,
)

from kd2_rules_mcp.console import utf8_stdout

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "kdbase" / "run"
# Имена совпадают с authoring/pack.py:25–27 (service использует pack_rules).
# БСП: ПравилаДляОбменаДанными/ManagerModule:669–693 — имена комплекта.
FILES = {
    "exchange": "ExchangeRules.xml",
    "correspondent": "CorrespondentExchangeRules.xml",
    "registration": "RegistrationRules.xml",
}


def binary_body(kind: str) -> str:
    """Хранилище раскрывается внутри базы; наружу уходит только запрошенная часть."""
    # БСП: ПравилаДляОбменаДанными/ManagerModule:794, 796, 804 —
    # ПравилаXML / ПравилаXMLКорреспондента; 363 — .Получить() даёт ДвоичныеДанные.
    field = "ПравилаXMLКорреспондента" if kind == "correspondent" else "ПравилаXML"
    return f"""Байты = Неопределено;
        Если З.{field} <> Неопределено Тогда Байты = З.{field}.Получить(); КонецЕсли;
        Данные.Вставить("binary", Байты <> Неопределено);
        Если Байты <> Неопределено Тогда
        Хеш = Новый ХешированиеДанных(ХешФункция.SHA256); Хеш.Добавить(Байты);
        Отпечаток = Base64Строка(Хеш.ХешСумма);
        Текст64 = СтрЗаменить(СтрЗаменить(Base64Строка(Байты), Символы.ПС, ""),
        Символы.ВК, "");
        Данные.Вставить("size", Байты.Размер());
        Данные.Вставить("length", СтрДлина(Текст64));
        Данные.Вставить("hash", Отпечаток); КонецЕсли;"""


def metadata_code(plan: str, kind: str) -> str:
    return json_code(
        rule_record_body(plan, kind)
        + rule_info_body(kind)
        + f"Если З.Выбран() Тогда {binary_body(kind)} КонецЕсли;"
    )


def chunk_code(plan: str, kind: str, offset: int, size: int, digest: str) -> str:
    if offset < 0 or size <= 0:
        raise ValueError("неверная граница части")
    return guarded(
        rule_record_body(plan, kind)
        + f"""Если Не З.Выбран() Тогда ВызватьИсключение "Запись правил исчезла"; КонецЕсли;
        Данные = Новый Структура; {binary_body(kind)}
        Если Не Данные.binary Тогда ВызватьИсключение "XML правил исчез"; КонецЕсли;
        Если Отпечаток <> {literal(digest)} Тогда
        ВызватьИсключение "Правила изменились во время чтения"; КонецЕсли;
        Результат = "OK " + Сред(Текст64, {offset + 1}, {size});"""
    )


def dump(args: argparse.Namespace, server: DataServer, lines: list[str]) -> Path:
    """Метаданные → части → проверка длины/хеша → XML → ZIP; нет записи — штатный факт."""
    out = (args.out or RUNS) / f"rules-{datetime.now():%Y%m%d-%H%M%S-%f}"
    stage(lines, "КАТАЛОГ", lambda: out.mkdir(parents=True))
    saved: list[Path] = []
    for kind, name in FILES.items():
        info = stage(
            lines, f"ЧТЕНИЕ {kind}", lambda k=kind: read_json(server, metadata_code(args.plan, k))
        )
        if not isinstance(info, dict) or not isinstance(info.get("present"), bool):
            raise ExchangeCheckError("некорректные сведения о записи правил")
        if not info["present"]:
            lines.append(f"ПРАВИЛА {kind}: нет записи в регистре, 0 байт")
            continue
        description = rule_description(info)
        if not info["binary"]:
            lines.append(f"ПРАВИЛА {kind}: {description}, XML отсутствует, 0 байт")
            continue

        def download(info=info, kind=kind) -> bytes:
            length, size = info["length"], info["size"]
            if not isinstance(length, int) or not isinstance(size, int) or min(length, size) < 0:
                raise ExchangeCheckError("некорректный размер правил")
            parts: list[str] = []
            for offset in range(0, length, args.chunk_size):
                count = min(args.chunk_size, length - offset)
                try:
                    part = server.run(chunk_code(args.plan, kind, offset, count, info["hash"]))
                except ExchangeCheckError as error:
                    raise ExchangeCheckError(f"часть {offset}:{offset + count}: {error}") from error
                if len(part) != count:
                    raise ExchangeCheckError(f"часть {offset}:{offset + count}: обрыв base64")
                parts.append(part)
            lines.append(f"ЧАСТИ {kind}: {len(parts)}")
            data = base64.b64decode("".join(parts), validate=True)
            digest = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
            if len(data) != size or digest != info["hash"]:
                raise ExchangeCheckError("размер или SHA256 снятых правил не совпал")
            return data

        data = stage(lines, f"СБОРКА {kind}", download)
        file = out / name
        stage(lines, f"ФАЙЛ {kind}", lambda f=file, d=data: f.write_bytes(d))
        saved.append(file)
        lines.append(f"ПРАВИЛА {kind}: {description}, {len(data)} байт")
    archive = out / "rules.zip"

    def pack() -> None:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            for file in saved:
                package.write(file, file.name)

    stage(lines, "ZIP", pack)
    lines.append(
        f"АРХИВ {archive}: {', '.join(file.name for file in saved) or 'пустой'}; "
        f"{'полный комплект' if len(saved) == 3 else 'неполный комплект'}"
    )
    return archive


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Снять действующие правила БСП из песочницы")
    parser.add_argument("--base", required=True, help="<проект>.<база>")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", type=Path, help="родитель каталога прогона")
    parser.add_argument("--chunk-size", type=int, default=48000, help="символов base64, кратно 4")
    args = parser.parse_args(argv)
    if not 4 <= args.chunk_size <= 96000 or args.chunk_size % 4:
        parser.error("--chunk-size: 4..96000 символов, кратно 4")
    return args


def main() -> None:
    utf8_stdout()
    args = parse_args()
    lines = [f"БАЗА {text_line(args.base)}", f"ПЛАН {text_line(args.plan)}"]
    ok = False
    try:
        server = stage(lines, "ПЕСОЧНИЦА", lambda: server_for(args.base))
        dump(args, server, lines)
        ok = True
    except Exception as error:
        lines.append(f"ОШИБКА {short_error(str(error))}")
    print("\n".join([*lines, f"ИТОГ {'OK' if ok else 'ОШИБКА'}", "КОНЕЦ"]))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
