"""Архив правил для загрузки в БСП (`rules_pack`).

БСП принимает архив правил двумя формами регистра `ПравилаДляОбменаДанными` (БСП 3.1.12;
строки ниже — модуль менеджера `InformationRegisters/ПравилаДляОбменаДанными`):

- «Правила конвертации объектов» (форма `ПравилаКонвертацииОбъектов`, модуль, 213) →
  `ЗагрузитьПравила` с `ЭтоАрхив`: ровно два файла `ExchangeRules.xml` и
  `CorrespondentExchangeRules.xml` (459, 480–498);
- «Загрузить правила синхронизации» (форма `ЗагрузитьПравилаСинхронизацииДанных`, модуль, 367) →
  `ЗагрузитьКомплектПравил`: ровно три файла, третий — `RegistrationRules.xml` (669–690).

Файлы считаются `НайтиФайлы(…, Истина)` по распакованному каталогу (469, 660), поэтому в архиве
нет ничего, кроме этих файлов, и нет каталогов. Файлы кладутся байт в байт — архив равен папке.
"""

import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kd2_rules_mcp.errors import Kd2Error, RulesFormatError
from kd2_rules_mcp.kd2.model import ExchangeRules, RegistrationRules
from kd2_rules_mcp.kd2.rules_io import load_rules

EXCHANGE = "ExchangeRules.xml"
CORRESPONDENT = "CorrespondentExchangeRules.xml"
REGISTRATION = "RegistrationRules.xml"
ROLES = {"exchange": EXCHANGE, "correspondent": CORRESPONDENT, "registration": REGISTRATION}

FORM_CONVERSION = (
    "Синхронизация данных → настройка обмена → «Правила конвертации объектов» → "
    "загрузить из архива (ExchangeRules.xml + CorrespondentExchangeRules.xml)"
)
FORM_SET = (
    "Синхронизация данных → «Загрузить правила синхронизации» — комплект из трёх файлов "
    "(конвертация, корреспондент, регистрация)"
)


@dataclass(frozen=True, slots=True)
class PackedFile:
    """Файл в архиве: имя внутри, откуда взят, размер и что в нём."""

    name: str
    source: Path
    size_bytes: int
    summary: dict[str, str]


@dataclass(frozen=True, slots=True)
class PackResult:
    """Собранный архив."""

    path: Path
    files: list[PackedFile]
    form: str
    warnings: list[str] = field(default_factory=list)


def collect(folder: Path | None, explicit: Mapping[str, Path | None]) -> dict[str, Path]:
    """Файлы комплекта по ролям: из папки (стандартные имена); явные пути заменяют найденные.

    Обязательны правила обмена и корреспондента; регистрации — если есть.
    """
    files: dict[str, Path] = {}
    if folder is not None:
        if not folder.is_dir():
            raise Kd2Error(f"«{folder}» — не каталог с правилами")
        for role, name in ROLES.items():
            if (folder / name).is_file():
                files[role] = folder / name
    for role, path in explicit.items():
        if path is not None:
            files[role] = path
    missing = [ROLES[role] for role in ("exchange", "correspondent") if role not in files]
    if missing:
        where = f" в «{folder}»" if folder is not None else ""
        raise Kd2Error(
            f"Нет файлов {', '.join(missing)}{where}: архив для БСП без них не загрузится"
        )
    return files


def pack_rules(
    files: Mapping[str, Path], destination: Path, *, overwrite: bool = False
) -> PackResult:
    """Проверяет файлы комплекта и пишет ZIP; существующий архив — только при `overwrite`."""
    documents: dict[str, ExchangeRules | RegistrationRules] = {}
    for role, path in files.items():
        if role not in ROLES:
            raise Kd2Error(f"Неизвестная роль файла «{role}»: {', '.join(ROLES)}")
        try:
            document = load_rules(path)
        except RulesFormatError as error:
            raise RulesFormatError(f"{ROLES[role]} ({path}): {error}") from error
        expected = RegistrationRules if role == "registration" else ExchangeRules
        if not isinstance(document, expected):
            kind = "правила регистрации" if expected is RegistrationRules else "правила обмена"
            raise RulesFormatError(f"{ROLES[role]} ({path}): ожидаются {kind}")
        documents[role] = document
    if destination.exists() and not overwrite:
        raise Kd2Error(f"Файл «{destination}» уже существует; замена только при overwrite=True")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for role in ROLES:
            if role in files:
                archive.write(files[role], ROLES[role])
    packed = [
        PackedFile(ROLES[role], files[role], files[role].stat().st_size, _summary(documents[role]))
        for role in ROLES
        if role in files
    ]
    form = FORM_SET if "registration" in files else FORM_CONVERSION
    return PackResult(destination, packed, form, _warnings(documents))


def _summary(document: ExchangeRules | RegistrationRules) -> dict[str, str]:
    name = str(document.root.values.get("Наименование", ""))
    if isinstance(document, ExchangeRules):
        return {"name": name, "source": document.source_name, "target": document.target_name}
    return {
        "name": name,
        "exchange_plan": document.exchange_plan,
        "configuration": _registration_configuration(document),
    }


def _registration_configuration(document: RegistrationRules) -> str:
    node = document.root.child("Конфигурация")
    return (node.text or "").strip() if node is not None else ""


def _warnings(documents: Mapping[str, ExchangeRules | RegistrationRules]) -> list[str]:
    """Несогласованность частей комплекта: БСП её не проверяет, а обмен идёт «не теми» правилами."""
    warnings: list[str] = []
    exchange, correspondent = documents["exchange"], documents["correspondent"]
    assert isinstance(exchange, ExchangeRules) and isinstance(correspondent, ExchangeRules)
    mirrored = (exchange.target_name, exchange.source_name)
    if (correspondent.source_name, correspondent.target_name) != mirrored:
        warnings.append(
            f"Правила корреспондента {correspondent.source_name} → {correspondent.target_name} "
            f"не зеркальны правилам {exchange.source_name} → {exchange.target_name}"
        )
    registration = documents.get("registration")
    if isinstance(registration, RegistrationRules):
        configuration = _registration_configuration(registration)
        if configuration and configuration != exchange.source_name:
            warnings.append(
                f"Правила регистрации — для конфигурации {configuration}, "
                f"а правила обмена выгружают из {exchange.source_name}"
            )
    return warnings
