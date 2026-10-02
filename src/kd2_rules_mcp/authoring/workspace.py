"""Рабочий проект правил: открытие XML, пустые правила для пары структур, запись в рабочую папку.

Писатель заголовка — `reference/kd2-cfg/DataProcessors/ВыгрузкаКонвертации/Ext/ObjectModule.bsl`
(`ВыгрузитьРеквизитыКонвертации`, `ВыгрузитьКонвертацию`). Ниже строки этого файла обозначены `ВК:`.
"""

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from kd2_rules_mcp.errors import Kd2Error, ProjectNotFoundError, WorkspacePathError
from kd2_rules_mcp.kd2.model import ExchangeRules, Node, RulesDocument
from kd2_rules_mcp.kd2.rules_io import SUPPORTED_FORMAT_VERSION, dump_rules, load_rules
from kd2_rules_mcp.kd2.xmlstyle import KD_STYLE
from kd2_rules_mcp.structures.store import StructureStore

# Значение заполнения Конвертации.РежимСовместимости
# (reference/kd2-cfg/Catalogs/Конвертации.xml:1404); имя значения пишет ВК:422.
_COMPATIBILITY = "РежимСовместимостиСБСП20"

# Код справочника — строка фиксированной длины 40
# (reference/kd2-cfg/Catalogs/Конвертации.xml:43, :46).
_CODE_LENGTH = 40


@dataclass(eq=False, slots=True)
class RulesProject:
    """Открытый рабочий проект: документ в памяти, путь источника и путь последнего сохранения."""

    id: str
    document: RulesDocument
    source_path: Path | None
    saved_path: Path | None = None


class RulesWorkspace:
    """Рабочая папка правил (design.md, Д5): проекты в памяти, запись только внутрь корня."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, RulesProject] = {}
        self._next_id = 0

    def open_rules(self, path: Path | str) -> RulesProject:
        """Открывает правила обмена или регистрации из XML по любому читаемому пути."""
        source = Path(path)
        return self._remember(load_rules(source), source.resolve())

    def create_exchange(
        self, store: StructureStore, source_id: str, target_id: str
    ) -> RulesProject:
        """Пустые правила обмена для пары структур.

        Неизвестный идентификатор структуры — `StructureNotFoundError` из кэша, проект не создаётся.
        """
        source = store.meta(source_id)
        target = store.meta(target_id)
        return self._remember(_empty_exchange(source, target), None)

    def get(self, project_id: str) -> RulesProject:
        """Рабочий проект по идентификатору; нет такого — ошибка со списком открытых."""
        project = self._projects.get(project_id)
        if project is not None:
            return project
        known = ", ".join(self._projects) or "нет открытых проектов"
        raise ProjectNotFoundError(f"Рабочего проекта «{project_id}» нет ({known})")

    def save(
        self,
        project_id: str,
        path: Path | str,
        *,
        overwrite: bool = False,
        allowed: Sequence[Path] = (),
    ) -> Path:
        """Пишет XML проекта внутрь рабочей папки (или разрешённой папки) и запоминает путь.

        Путь относительный к корню или абсолютный. После `resolve` (включая `..` и симлинки)
        файл должен лежать внутри корня или одной из `allowed` (папки живых правил проектов),
        иначе `WorkspacePathError`. Существующий файл заменяется только при `overwrite=True`.
        """
        project = self.get(project_id)
        destination = self._destination(path, allowed)
        if destination.is_file() and not overwrite:
            raise Kd2Error(
                f"Файл «{destination}» уже существует; повторная запись только при overwrite=True"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(dump_rules(project.document))
        project.saved_path = destination
        return destination

    def ids(self) -> list[str]:
        """Идентификаторы открытых проектов в порядке открытия."""
        return list(self._projects)

    def add(self, document: RulesDocument) -> RulesProject:
        """Новый проект из документа, собранного сервером (правила регистрации, черновик)."""
        return self._remember(document, None)

    def resolve(self, path: Path | str, allowed: Sequence[Path] = ()) -> Path:
        """Проверенный путь внутри рабочей папки или `allowed`; вне их — `WorkspacePathError`."""
        return self._destination(path, allowed)

    def _remember(self, document: RulesDocument, source_path: Path | None) -> RulesProject:
        self._next_id += 1
        project = RulesProject(str(self._next_id), document, source_path)
        self._projects[project.id] = project
        return project

    def _destination(self, path: Path | str, allowed: Sequence[Path] = ()) -> Path:
        roots = [self.root.resolve(), *(Path(folder).resolve() for folder in allowed)]
        candidate = Path(normalize_relative(os.fspath(path)))
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if not any(_is_inside(resolved, root) for root in roots):
            raise WorkspacePathError(
                f"Сохранение «{resolved}» отклонено: путь вне рабочей папки"
                f"{' и папок правил проектов' if allowed else ''}. "
                f"Разрешено: {', '.join(str(root) for root in roots)}"
            )
        return resolved


def normalize_relative(path: str) -> str:
    """Обратная косая в относительном пути — разделитель каталогов, не символ имени.

    Абсолютный путь Windows (`C:\\…`, `\\\\сервер\\…`) и POSIX (`/…`) не меняется:
    его переводит соответствие путей агента и сервера.
    """
    if PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute():
        return path
    return path.replace("\\", "/")


def _is_inside(path: Path, root: Path) -> bool:
    """Разрешённый путь лежит внутри корня и не совпадает с самим корнем."""
    folded_path = os.path.normcase(str(path))
    folded_root = os.path.normcase(str(root))
    if folded_path == folded_root:
        return False
    try:
        Path(folded_path).relative_to(folded_root)
    except ValueError:
        return False
    return True


def _empty_exchange(source: dict[str, str], target: dict[str, str]) -> ExchangeRules:
    """Заголовок новых правил обмена без правил — как его пишет `ВыгрузитьКонвертацию`.

    Контейнеры разделов в модели не создаются: писатель всё равно открывает их пустыми
    (ВК:1457–1490 — ПКО, ПВД, ПОД, алгоритмы, запросы; ВК:517 — обработки). У новой
    конвертации включена выгрузка параметров по версии 2.01
    (`Catalogs/Конвертации/Ext/ObjectModule.bsl:22-24`), поэтому пустой контейнер
    `Параметры` тоже пишется (ВК:482–512). Сериализатор выводит эти контейнеры по
    политике ALWAYS, даже если узла в модели нет.
    """
    root = Node.new("exchange_rules", "ПравилаОбмена")
    version = Node.new("format_version", "ВерсияФормата")
    # мВерсияФормата (ВК:2602), текст тега — ВыгрузитьДанныеВерсии (ВК:420).
    version.text = SUPPORTED_FORMAT_VERSION
    version.attrs["РежимСовместимости"] = _COMPATIBILITY
    root.children["ВерсияФормата"] = version
    root.values["Ид"] = _new_code()
    root.values["Наименование"] = _conversion_name(
        source.get("config_name", ""), target.get("config_name", "")
    )
    # ДатаОбновления = ТекущаяДата() (ВК:397), в XML — XMLСтрока этой даты (ВК:405, ВК:90).
    root.values["ДатаВремяСоздания"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    root.children["Источник"] = _config_side("Источник", source)
    root.children["Приемник"] = _config_side("Приемник", target)
    return ExchangeRules(root, KD_STYLE)


def _new_code() -> str:
    """Новый код конвертации: GUID, дополненный пробелами до длины кода.

    `СгенерироватьУникальныйКод` — `Catalogs/Конвертации/Ext/ObjectModule.bsl:10`
    (`Строка(Новый УникальныйИдентификатор())`). Писатель выводит `Строка(Код)` (ВК:403);
    код фиксированной длины хранится с хвостовыми пробелами.
    """
    return str(uuid.uuid4()).ljust(_CODE_LENGTH)


def _conversion_name(source_name: str, target_name: str) -> str:
    """Наименование новой конвертации: «Источник --> Приемник».

    `глНаименованиеКонвертации` (`CommonModules/ОбщегоНазначения/Ext/Module.bsl:141`).
    Пустое наименование перед записью заполняется так
    (`Catalogs/Конвертации/Ext/ObjectModule.bsl:45-47`), писатель выводит его (ВК:404).
    Представление конфигурации — её наименование, а выгрузка структуры кладёт туда имя
    конфигурации (`MD83Exp/ВыгрузкаМетаданных/Ext/ObjectModule.bsl:114-115`), то есть `config_name`.
    """
    return f"{source_name.strip()} --> {target_name.strip()}".strip()


def _config_side(tag: str, meta: dict[str, str]) -> Node:
    """`Источник` или `Приемник`: имя текстом, синоним и версия из meta структуры.

    Текст — `Конфигурация.Имя` (ВК:378), атрибуты синонима и версии — ВК:380–381.
    Версии платформы в структуре нет: писатель всегда ставит атрибут (ВК:379) из
    `ПолучитьПредставлениеПриложения` (ВК:2588–2596). Значение заполнения реквизита
    `Конфигурации.Приложение` не задано (`Catalogs/Конфигурации.xml:332`), пустое
    приложение даёт пустую строку (ВК:2595).
    """
    node = Node.new("config", tag)
    node.text = meta.get("config_name", "")
    node.attrs["ВерсияПлатформы"] = ""
    node.attrs["ВерсияКонфигурации"] = meta.get("config_version", "")
    node.attrs["СинонимКонфигурации"] = meta.get("config_synonym", "")
    return node
