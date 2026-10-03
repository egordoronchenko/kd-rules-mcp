"""Повторяемый перечень модулей КД 3 и пакетов EnterpriseData из выгрузок проектов.

Счётчики текстовые, не заменяют разбор BSL и проверку правил во время обмена.
"""

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from kd2_rules_mcp.console import utf8_stdout
from kd2_rules_mcp.errors import RulesFormatError
from kd2_rules_mcp.kd2.canonical import parse_xml
from kd2_rules_mcp.kd2.rules_io import load_registration_rules
from kd2_rules_mcp.projects import load_catalog, load_local, resolve

ROOT = Path(__file__).resolve().parents[1]
MANAGER_PREFIX = "МенеджерОбменаЧерезУниверсальныйФормат"
# Объявления процедур и функций в начале строки; не вызовы и не комментарии.
DECLARATION = re.compile(r"^[ \t]*(?:Процедура|Функция)\s+(\w+)\s*\(", re.M | re.I)
# Только объявления процедур правил; функции с похожим именем не считаются правилами.
PROCEDURE_DECLARATION = re.compile(r"^[ \t]*Процедура\s+(\w+)\s*\(", re.M | re.I)
# Тело именованной функции версии; не одноимённые вызовы, не вычисляет выражения.
VERSION_FUNCTION = re.compile(
    r"^[ \t]*Функция\s+(ВерсияФорматаМенеджераОбмена|ВерсияБиблиотеки)\s*\([^)]*\)"
    r"(.*?)^[ \t]*КонецФункции",
    re.M | re.S | re.I,
)
# Литеральный возврат версии в теле функции; не переменные и не комментарии.
VERSION_RETURN = re.compile(r'^[ \t]*Возврат\s+"([\d.]+)"\s*;', re.M | re.I)
# Версия БСП в описании подсистемы; не другие поля и не комментарии.
BSP_VERSION = re.compile(r'^[ \t]*Описание\.Версия\s*=\s*"([\d.]+)"', re.M | re.I)
# Присваивание имени ПКПД; не объявления общей процедуры заполнения и не вызовы.
PREDEFINED = re.compile(r'^[ \t]*\w+\.ИмяПКПД\s*=\s*"[^"\n]+"', re.M | re.I)
# Вызов ДобавитьПКС; не объявление (удаляется отдельно), не часть длинного имени.
# Это текстовый подсчёт: строковые литералы с таким текстом могут дать ложное совпадение.
PROPERTY_CALL = re.compile(r"(?<!\w)ДобавитьПКС\s*\(", re.I)
# Директивы областей; не комментарии. Вложенные области учитываются стеком.
REGION = re.compile(r"^[ \t]*#(Область\s+(\w+)|КонецОбласти)\b", re.M | re.I)
# Ссылка на общий модуль менеджера; не часть длинного идентификатора.
# Строковое имя тоже считается ссылкой; семантика использования не проверяется.
MANAGER_REFERENCE = re.compile(r"\bМенеджерОбменаЧерезУниверсальныйФормат\w*\b", re.I)
# Вызов поставщика версий из общего модуля; не динамические вызовы и не другие методы.
VERSION_PROVIDER = re.compile(r"\b(\w+)\.ПриПолученииДоступныхВерсийФормата\s*\(", re.I)
# Только тело процедуры поставщика; не другие процедуры того же модуля.
PROVIDER_BODY = re.compile(
    r"^[ \t]*Процедура\s+ПриПолученииДоступныхВерсийФормата\s*\([^)]*\)"
    r"(.*?)^[ \t]*КонецПроцедуры",
    re.M | re.S | re.I,
)
# Только отдельный числовой суффикс имени пакета; не версия из Namespace.
PACKAGE_VERSION = re.compile(r"^(?:EnterpriseData|EnterpriseDataExchange)_([\d_]+)$")
# Однострочные комментарии отсекаются перед подсчётом вызовов; строки не разбираются.
LINE_COMMENT = re.compile(r"//[^\n]*")
EVENTS = (
    "ПриОтправкеДанных",
    "ПриКонвертацииДанныхXDTO",
    "ПередЗаписьюПолученныхДанных",
    "ПослеЗагрузкиВсехДанных",
    "ПослеКонвертацииОбъекта",
    "ПриУдаленииОбъектаИБ",
    "ПриОбработке",
    "ВыборкаДанных",
    "АлгоритмПоиска",
)


@dataclass
class DumpInventory:
    """Одна выгрузка, включая расширения отдельно от основной конфигурации."""

    project: str
    configuration: str
    dump: str
    managers: list[dict[str, str | int]] = field(default_factory=list)
    plans: list[dict[str, str | int]] = field(default_factory=list)
    packages: list[dict[str, str | int]] = field(default_factory=list)
    reader: dict[str, str | int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def literal_version(text: str, name: str) -> str:
    """Версия только из литерального возврата нужной функции."""
    for match in VERSION_FUNCTION.finditer(text):
        if match[1].casefold() == name.casefold():
            value = VERSION_RETURN.search(match[2])
            if value:
                return value[1]
    return "не определена"


def manager_counts(text: str) -> dict[str, str | int]:
    """Счётчики по шаблонам дайджеста §1.1, §1.5–1.7, без парсера BSL."""
    names = DECLARATION.findall(text)
    procedures = PROCEDURE_DECLARATION.findall(text)
    # Объявление ДобавитьПКС не является вызовом; комментарии исключены эвристически.
    calls = LINE_COMMENT.sub("", text)
    calls = DECLARATION.sub("", calls)
    stack: list[str] = []
    algorithms = 0
    for line in text.splitlines():
        region = REGION.match(line)
        if region:
            if region[2]:
                stack.append(region[2].casefold())
            elif stack:
                stack.pop()
        elif "алгоритмы" in stack and DECLARATION.match(line):
            algorithms += 1
    return {
        "lines": len(text.splitlines()),
        "version": literal_version(text, "ВерсияФорматаМенеджераОбмена"),
        "pko": sum(n.casefold().startswith("добавитьпко_") for n in procedures),
        "pod": sum(n.casefold().startswith("добавитьпод_") for n in procedures),
        "pkpd": len(PREDEFINED.findall(text)),
        "pks": len(PROPERTY_CALL.findall(calls)),
        "handlers": sum(
            n.casefold().startswith(("пко_", "пкс_", "под_"))
            and any(n.casefold().endswith("_" + e.casefold()) for e in EVENTS)
            for n in names
        ),
        "algorithms": algorithms,
    }


def scan_dump(project: str, configuration: str, folder: Path, dump: str) -> DumpInventory:
    """Читает одну выгрузку; ошибка файла не скрывает остальные найденные объекты."""
    result = DumpInventory(project, configuration, dump)
    root = resolve(folder, dump)

    def relative(path: Path) -> str:
        return path.relative_to(folder).as_posix()

    def failure(path: Path, error: Exception) -> None:
        # Не выводим текст исключения: он может содержать абсолютный личный путь или XML.
        reason = (
            "файл отсутствует" if isinstance(error, FileNotFoundError) else type(error).__name__
        )
        result.errors.append(f"{relative(path)} — не прочитано: {reason}")

    if not root.is_dir():
        failure(root, FileNotFoundError())
        return result
    try:
        modules = sorted((root / "CommonModules").glob(MANAGER_PREFIX + "*"))
        for module in modules:
            if not module.is_dir():
                continue
            path = module / "Ext/Module.bsl"
            try:
                counts = manager_counts(path.read_text(encoding="utf-8-sig"))
                result.managers.append({"name": module.name, "file": relative(path), **counts})
            except (OSError, UnicodeError) as error:
                failure(path, error)

        for plan in sorted((root / "ExchangePlans").glob("*")):
            if not plan.is_dir():
                continue
            template = plan / "Templates/ПравилаРегистрации/Ext/Template.txt"
            if not template.is_file():
                continue
            manager = plan / "Ext/ManagerModule.bsl"
            try:
                text = manager.read_text(encoding="utf-8-sig")
                references = sorted(set(MANAGER_REFERENCE.findall(LINE_COMMENT.sub("", text))))
                providers: list[str] = []
                for name in sorted(set(VERSION_PROVIDER.findall(LINE_COMMENT.sub("", text)))):
                    provider = root / "CommonModules" / name / "Ext/Module.bsl"
                    try:
                        provider_text = provider.read_text(encoding="utf-8-sig")
                        body = PROVIDER_BODY.search(provider_text)
                        if body:
                            references.extend(
                                MANAGER_REFERENCE.findall(LINE_COMMENT.sub("", body[1]))
                            )
                            providers.append(relative(provider))
                    except (OSError, UnicodeError) as error:
                        failure(provider, error)
                references = sorted(set(references))
                if not references:
                    continue
                rules = load_registration_rules(template)
                result.plans.append(
                    {
                        "name": plan.name,
                        "file": relative(template),
                        "manager": relative(manager),
                        "modules": ", ".join(references),
                        "providers": ", ".join(providers) or "—",
                        "rules": sum(n.tag == "Правило" for n in rules.rules()),
                    }
                )
            except (OSError, UnicodeError, RulesFormatError) as error:
                failure(template if manager.is_file() else manager, error)

        for package in sorted((root / "XDTOPackages").glob("*")):
            if not package.is_dir() or not package.name.startswith(
                ("EnterpriseData_", "EnterpriseDataExchange_")
            ):
                continue
            path = package / "Ext/Package.bin"
            try:
                data = path.read_bytes()
                # XML допускает BOM UTF-8/UTF-16; прочие первые байты считаем двоичным файлом.
                prefix = data[:128]
                if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
                    is_xml = prefix.decode("utf-16", errors="ignore").lstrip().startswith("<")
                else:
                    is_xml = prefix.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<")
                match = PACKAGE_VERSION.fullmatch(package.name)
                item: dict[str, str | int] = {
                    "name": package.name,
                    "file": relative(path),
                    "version": match[1].replace("_", ".") if match else "не определена",
                    "format": "XML-текст" if is_xml else "двоичный",
                    "object_types": "—",
                    "value_types": "—",
                }
                if is_xml:
                    tree = parse_xml(data)
                    for key, tag in (("object_types", "objectType"), ("value_types", "valueType")):
                        item[key] = sum(
                            isinstance(n.tag, str) and etree.QName(n).localname == tag
                            for n in tree.iter()
                        )
                result.packages.append(item)
            except (OSError, UnicodeError, RulesFormatError) as error:
                failure(path, error)

        reader = root / "CommonModules/ОбменДаннымиXDTOСервер/Ext/Module.bsl"
        result.reader = {"file": relative(reader), "present": "нет", "lines": "—", "bsp": "—"}
        if reader.is_file():
            try:
                text = reader.read_text(encoding="utf-8-sig")
                result.reader.update(
                    present="да", lines=len(text.splitlines()), bsp="не определена"
                )
                version_file = root / "CommonModules/ОбновлениеИнформационнойБазыБСП/Ext/Module.bsl"
                result.reader["version_file"] = relative(version_file)
                if version_file.is_file():
                    try:
                        version_text = version_file.read_text(encoding="utf-8-sig")
                    except (OSError, UnicodeError) as error:
                        failure(version_file, error)
                        return result
                    version = literal_version(version_text, "ВерсияБиблиотеки")
                    match = BSP_VERSION.search(version_text)
                    result.reader["bsp"] = (
                        match[1] if version == "не определена" and match else version
                    )
            except (OSError, UnicodeError) as error:
                failure(reader, error)
    except OSError as error:
        failure(root, error)
    return result


def inventory(repository: Path = ROOT, project_id: str | None = None) -> list[DumpInventory]:
    """Пути только из штатного каталога и личных настроек, без подключения к базам."""
    catalog = load_catalog(repository / "projects.yaml")
    local = load_local(repository / "projects.local.yaml")
    projects = [catalog.project(project_id)] if project_id else list(catalog.projects.values())
    results: list[DumpInventory] = []
    for project in projects:
        folder = local.project_dirs.get(project.id)
        for configuration in project.configurations.values():
            for dump in (configuration.dump, *configuration.extensions):
                if folder is None:
                    result = DumpInventory(project.id, configuration.id, dump)
                    result.errors.append("не прочитано: папка проекта не задана")
                    results.append(result)
                else:
                    results.append(scan_dump(project.id, configuration.id, folder, dump))
    return results


def render_markdown(results: list[DumpInventory]) -> str:
    """Таблицы с относительными путями и явными ошибками чтения."""
    lines = [
        "# Корпус КД 3 / EnterpriseData",
        "",
        "Источник: projects.yaml + projects.local.yaml; выгрузки только читаются.",
        "",
        "Счётчики по тексту BSL, без выполнения и проверки семантики. "
        "ПКПД — присваивания ИмяПКПД; алгоритмы — объявления в области Алгоритмы "
        "(включая служебные). Обработчики — объявления ПКО_/ПКС_/ПОД_ с суффиксом события, "
        "включая функции ВыборкаДанных. Версия модуля отличается от версии пакета EnterpriseData.",
        "",
    ]

    def table(title: str, columns: list[tuple[str, str]], rows: list[dict[str, str | int]]) -> None:
        lines.extend([f"### {title}", ""])
        if not rows:
            lines.extend(["Не обнаружено.", ""])
            return
        lines.append("| " + " | ".join(label for _, label in columns) + " |")
        lines.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows:
            lines.append(
                "| "
                + " | ".join(
                    str(row.get(key, "—")).replace("|", "\\|").replace("\n", " ")
                    for key, _ in columns
                )
                + " |"
            )
        lines.append("")

    for result in results:
        lines.extend([f"## {result.project} / {result.configuration} / {result.dump}", ""])
        lines.extend([error + "\n" for error in result.errors])
        table(
            "Модули менеджеров обмена",
            [
                ("name", "Имя"),
                ("file", "Файл"),
                ("lines", "Строк"),
                ("version", "Версия модуля"),
                ("pko", "ПКО"),
                ("pod", "ПОД"),
                ("pkpd", "ПКПД"),
                ("pks", "ПКС"),
                ("handlers", "Обработчики"),
                ("algorithms", "Алгоритмы"),
            ],
            result.managers,
        )
        table(
            "Планы обмена",
            [
                ("name", "План"),
                ("file", "Правила регистрации"),
                ("rules", "Правил"),
                ("manager", "Модуль плана"),
                ("modules", "Менеджеры обмена"),
                ("providers", "Поставщик версий"),
            ],
            result.plans,
        )
        table(
            "XDTO-пакеты",
            [
                ("name", "Имя"),
                ("version", "Версия из имени"),
                ("file", "Файл"),
                ("format", "Формат"),
                ("object_types", "objectType"),
                ("value_types", "valueType"),
            ],
            result.packages,
        )
        table(
            "Читатель БСП",
            [
                ("file", "Файл"),
                ("present", "Есть"),
                ("lines", "Строк"),
                ("bsp", "Версия БСП"),
                ("version_file", "Источник версии"),
            ],
            [result.reader] if result.reader else [],
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    utf8_stdout()
    parser = argparse.ArgumentParser(description="Инвентаризация корпуса КД 3 / EnterpriseData")
    parser.add_argument("--project", help="идентификатор проекта из projects.yaml")
    parser.add_argument(
        "--write", type=Path, metavar="файл.md", help="записать перечень в UTF-8, LF"
    )
    args = parser.parse_args()
    markdown = render_markdown(inventory(project_id=args.project))
    if args.write:
        args.write.write_text(markdown, encoding="utf-8", newline="\n")
    else:
        print(markdown, end="")


if __name__ == "__main__":
    main()
