"""Разрешённая доставка в выгрузку проекта и квитанция в рабочей папке."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kd_rules_mcp.authoring.ed.extension_merge import (
    MergeResult,
    merge_extension,
    prop,
    refuse,
    xml,
)
from kd_rules_mcp.authoring.ed.hook import bsl_string
from kd_rules_mcp.authoring.ed.manifest import json_bytes, sha256
from kd_rules_mcp.authoring.ed.registration_delivery import read_plan_host
from kd_rules_mcp.projects import resolve
from kd_rules_mcp.structures.store import dump_fingerprint

_active_staging: ContextVar[frozenset[Path]] = ContextVar("extension_staging", default=frozenset())


def _user_instruction(original: str) -> str:
    """Сохраняет проверки правил, исключая управление целым расширением пользователя."""
    for heading in (
        "## Шаг 2. Установите расширение",
        "## 2. Установите расширение",
        "## Если переносите руками или через хранилище",
        "## Обновление комплекта",
        "## Как вернуть типовые правила",
    ):
        original = re.sub(
            re.escape(heading) + r"\n[\s\S]*?(?=^## |\Z)", "", original, flags=re.MULTILINE
        )
    original = re.sub(
        r"Версия установленного расширения:\n[\s\S]*?(?=Выбор модуля и число правил)", "", original
    )
    original = re.sub(r"(?m)^Расширение отключайте[^\n]*(?:\n(?!\n)[^\n]*)*\n?", "", original)
    original = original.replace("расширение ставить нельзя", "доставлять комплект нельзя")
    original = original.replace(
        "После обновления типовой конфигурации повторите шаги 2 и 5:",
        "После обновления типовой конфигурации повторите доставку и проверку выбора модуля:",
    )
    return original


def delivery_options(raw: object) -> dict[str, str]:
    """Прежнее строковое умолчание оставлено совместимым для авторинга overlay."""
    if raw in (None, "extension"):
        return {"mode": "new_extension"}
    if not isinstance(raw, dict) or set(raw) - {"mode", "extension"}:
        raise ValueError("delivery: нужны mode и extension")
    mode = raw.get("mode", "new_extension")
    if mode not in ("new_extension", "user_extension"):
        raise ValueError("delivery.mode: new_extension или user_extension")
    if mode == "user_extension":
        name = raw.get("extension")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("delivery.extension: нужно имя расширения")
        return {"mode": mode, "extension": name}
    if "extension" in raw:
        raise ValueError("delivery.extension применяется только к user_extension")
    return {"mode": mode}


def extension_target(service: Any, options: dict[str, str], base: Path) -> Path:
    """Имя ищется только среди расширений конфигурации с этой основной выгрузкой."""
    catalog = service._catalog()
    matches = []
    for project_id, project in catalog.projects.items():
        folder = service.settings.project_dirs.get(project_id)
        if folder is None:
            continue
        for config in project.configurations.values():
            configured_base = service._read_path(
                service._host(resolve(folder, config.dump))
            ).resolve()
            if configured_base != base.resolve():
                continue
            for relative in config.extensions:
                target = service._read_path(service._host(resolve(folder, relative)))
                path_name = relative.replace("\\", "/").rstrip("/").split("/")[-1]
                description = target / "Configuration.xml"
                name = (
                    prop(xml(description.read_bytes()), "Name")
                    if description.is_file()
                    else path_name
                )
                if options["extension"] not in (name, path_name):
                    continue
                if relative not in config.writable_extensions:
                    refuse("Расширение не включено в writable_extensions", "delivery_not_writable")
                matches.append(target)
    unique = list(dict.fromkeys(matches))
    if len(unique) != 1:
        refuse(
            "Расширение не найдено или имя неоднозначно в projects.yaml",
            "delivery_unknown_extension",
        )
    return unique[0]


def _safe(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()) or any(
        p.is_symlink() or p.is_junction() for p in (path, *path.parents)
    ):
        refuse("Путь доставки выходит за разрешённую папку", "delivery_invalid_extension")


def _stage(path: Path, content: bytes) -> Path:
    """Временный файл на том же томе; до замены целевой файл не меняется."""
    descriptor, temporary = tempfile.mkstemp(prefix=".kd2-delivery-", dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temp
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def atomic_files(
    contents: dict[Path, bytes],
    verify: Callable[[], None],
    verify_staged: Callable[[set[Path]], None] | None = None,
    after_replace: Callable[[], dict[Path, bytes]] | None = None,
) -> None:
    """Подготовка всего набора и откат замен при исключении, без удаления чужих файлов."""
    staged, backups, replaced, created_dirs = {}, {}, [], []
    try:
        verify()
        for path, content in contents.items():
            if path.is_file() and path.read_bytes() == content:
                continue
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_dirs.append(directory)
            staged[path] = _stage(path, content)
            backups[path] = _stage(path, path.read_bytes()) if path.is_file() else None
        if verify_staged is None:
            verify()
        else:
            ignored = set(staged.values()) | {p for p in backups.values() if p is not None}
            token = _active_staging.set(frozenset(ignored))
            try:
                verify_staged(ignored)
            finally:
                _active_staging.reset(token)
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
        if after_replace is not None:
            for path, content in after_replace().items():
                if path not in backups:
                    backups[path] = _stage(path, path.read_bytes()) if path.is_file() else None
                temporary = _stage(path, content)
                staged[path] = temporary
                os.replace(temporary, path)
                if path not in replaced:
                    replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            backup = backups[path]
            if backup is None:
                path.unlink()
            else:
                os.replace(backup, path)
        raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        for directory in reversed(created_dirs):
            with suppress(OSError):
                directory.rmdir()


@dataclass
class UserDelivery:
    root: Path
    slot: Path
    merge: MergeResult
    files: dict[str, bytes]
    previous: dict[str, bytes]

    @property
    def fingerprint(self) -> dict:
        return {
            "root": str(self.root),
            "inputs": self.merge.inputs,
            "outputs": {p: sha256(b) for p, b in self.files.items()},
            "previous": {p: sha256(b) for p, b in self.previous.items()},
        }

    def verify(self, ignored: set[Path] | None = None) -> None:
        for name, expected in self.merge.inputs.items():
            path = self.root / name
            _safe(self.root, path)
            current = sha256(path.read_bytes()) if path.is_file() else None
            if current != expected:
                refuse("Выгрузка изменилась после preview", "delivery_stale")
        if _previous(self.slot, ignored) != self.previous:
            refuse("Комплект доставки изменился после preview", "delivery_stale")

    def write(
        self,
        verify: Callable[[], None],
        input_receipt: Callable[[], dict] | None = None,
    ) -> str:
        def check():
            verify()
            self.verify()

        def check_staged(ignored):
            verify()
            self.verify(ignored)

        contents = {self.root / p: b for p, b in self.merge.files.items()}
        contents.update({self.slot / p: b for p, b in self.files.items()})
        for path in contents:
            _safe(self.root if path.is_relative_to(self.root) else self.slot, path)
        unchanged = all(p.is_file() and p.read_bytes() == b for p, b in contents.items())

        def finish():
            manifest = json.loads(self.files["manifest.json"])
            manifest["delivery_inputs"] = input_receipt() if input_receipt else None
            self.files["manifest.json"] = json_bytes(manifest)
            return {self.slot / "manifest.json": self.files["manifest.json"]}

        atomic_files(contents, check, check_staged, finish if input_receipt else None)
        return "unchanged" if unchanged else "written"


def _previous(slot: Path, ignored: set[Path] | None = None) -> dict[str, bytes]:
    if not slot.exists():
        return {}
    result = {}
    for path in slot.rglob("*"):
        _safe(slot, path)
        if path.is_file() and path not in (ignored or set()):
            result[path.relative_to(slot).as_posix()] = path.read_bytes()
    if result:
        try:
            manifest = json.loads(result["manifest.json"])
            if manifest["delivery"]["mode"] != "user_extension" or manifest["file_hashes"] != {
                p: sha256(b) for p, b in result.items() if p != "manifest.json"
            }:
                raise ValueError
        except (KeyError, ValueError, TypeError) as error:
            refuse("Папка комплекта занята или изменена вручную: " + str(slot))
            raise AssertionError from error
    return result


def prepare_user_delivery(
    service: Any, raw: object, base: Path, generated: dict[str, bytes], slot: Path, owner: str
) -> UserDelivery:
    options = delivery_options(raw)
    root = extension_target(service, options, base)
    service._safe_path(slot)
    previous = _previous(slot)
    owned = {}
    if previous:
        manifest = json.loads(previous["manifest.json"])
        if manifest["owner"] != owner or manifest["delivery"]["root"] != str(root):
            refuse("Комплект принадлежит другой доставке")
        owned = {
            p.removeprefix("extension/"): b
            for p, b in previous.items()
            if p.startswith("extension/")
        }
    inputs = {
        p.removeprefix("extension/"): b for p, b in generated.items() if p.startswith("extension/")
    }
    merged = merge_extension(root, base, inputs, owned)
    files = {p: b for p, b in previous.items() if p != "manifest.json"}
    files.update(
        {
            p: b
            for p, b in generated.items()
            if not p.startswith("extension/")
            and p not in ("manifest.json", "instruction.md", "ИНСТРУКЦИЯ.md")
        }
    )
    files.update({"extension/" + p: b for p, b in merged.files.items()})
    rows = merged.rows()
    source_manifest = json.loads(generated.get("manifest.json", b"{}"))
    original = generated.get("instruction.md", generated.get("ИНСТРУКЦИЯ.md", b"")).decode("utf-8")
    original = _user_instruction(original)
    old_name = source_manifest.get("extension_name") or source_manifest.get("identity", {}).get(
        "name"
    )
    if old_name:
        target_name = prop(xml((root / "Configuration.xml").read_bytes()), "Name")
        original = original.replace(bsl_string(old_name), bsl_string(target_name))
        original = original.replace("`" + old_name + "`", "`" + target_name + "`")
    listing = "\n".join("- `" + row["path"] + "`" for row in rows)
    instruction = (
        f"# Доставка в расширение `{options['extension']}`\n\n"
        f"Изменения в выгрузке расширения `{options['extension']}`:\n\n{listing}\n\n"
        "Далее обычным путём команды: хранилище/загрузка из файлов.\n\n"
        "Откат: `git checkout -- <файлы>` для изменённых файлов; новые файлы удалите "
        "по описи после проверки diff. Удаление сущностей при повторной сборке "
        "не выполняется.\n\n" + original
    )
    files["instruction.md"] = instruction.encode("utf-8")
    delivery = {**options, "root": str(root), "files": rows}
    if previous and "delivery_inputs" in json.loads(previous["manifest.json"]):
        source_manifest["delivery_inputs"] = json.loads(previous["manifest.json"])[
            "delivery_inputs"
        ]
    # Полный исходный манифест сохраняет решения писателя и сведения проверок.
    source_manifest.update(
        owner=owner, delivery=delivery, file_hashes={p: sha256(b) for p, b in sorted(files.items())}
    )
    files["manifest.json"] = json_bytes(source_manifest)
    return UserDelivery(root, slot, merged, files, previous)


def manager_input_receipt(service: Any, metadata: dict) -> dict:
    """После собственной доставки сохраняет точный отпечаток всех входных слоёв."""
    args = metadata["arguments"]
    roots = tuple(service._read_path(p) for p in (args["configuration_path"], *args["extensions"]))
    return input_receipt(roots, metadata["dump_fingerprint"])


def input_receipt(roots: tuple[Path, ...], before: str) -> dict:
    return {
        "before": before,
        "after": dump_fingerprint(roots),
        "roots": [str(p.resolve()) for p in roots],
    }


def delivered_structure_matches(
    service: Any, roots: tuple[Path, ...], before: str, after: str
) -> bool:
    """Разрешает структуру прежних слоёв только после зафиксированной собственной доставки."""
    folder = service.workspace.root.absolute() / "ed-authoring"
    for path in folder.glob("*/manifest.json"):
        _safe(folder, path)
        manifest = json.loads(path.read_bytes())
        if manifest.get("delivery_inputs") == {
            "before": before,
            "after": after,
            "roots": [str(p.resolve()) for p in roots],
        }:
            _previous(path.parent, set(_active_staging.get()))
            return True
    return False


def registration_delivery_host(
    host: Any,
    root: Path,
    plan: str,
    extensions: tuple[Path, ...],
    target: Path | None,
    object_names: tuple[str, ...] | None,
) -> Any:
    """Состав и источники берёт со всеми слоями; проверки чужих реквизитов — без целевого."""
    if target is None or target.resolve() not in {p.resolve() for p in extensions}:
        return host
    ownership = read_plan_host(
        root,
        plan,
        extensions=tuple(p for p in extensions if p.resolve() != target.resolve()),
        object_names=object_names,
        check_plan_content=True,
    )
    return replace(
        host, properties=ownership.properties, extension_attributes=ownership.extension_attributes
    )


def delivered_input_fingerprint(service: Any, project_id: str, metadata: dict) -> str | None:
    """Допускает только точное итоговое состояние собственной доставки, без rebind."""
    args = metadata["arguments"]
    folder = service.workspace.root.absolute() / "ed-authoring"
    for path in folder.glob(args["identity"]["name"] + "-*/manifest.json"):
        _safe(folder, path)
        manifest = json.loads(path.read_bytes())
        receipt = manifest.get("delivery_inputs")
        if (
            manifest.get("owner") == project_id
            and isinstance(receipt, dict)
            and receipt.get("before") == metadata["dump_fingerprint"]
        ):
            _previous(path.parent, set(_active_staging.get()))
            return receipt.get("after")
    return None
