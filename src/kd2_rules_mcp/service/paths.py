"""Пути агента и настройки сервера из окружения (`KD2_*`)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from kd2_rules_mcp.errors import Kd2Error
from kd2_rules_mcp.projects import load_local, parse_project_dirs


@dataclass(frozen=True, slots=True)
class PathMap:
    """Соответствие префиксов путей агента и локальных путей сервера."""

    pairs: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, text: str) -> "PathMap":
        """`путь_агента=локальный;…`; пустая строка — пути не переводятся."""
        pairs: list[tuple[str, str]] = []
        for chunk in text.split(";"):
            if not chunk.strip():
                continue
            host, sep, local = chunk.partition("=")
            if not sep or not host.strip() or not local.strip():
                raise Kd2Error(f"Неверный элемент KD2_PATH_MAP: «{chunk}»")
            pairs.append((host.strip(), local.strip()))
        # Длинный префикс раньше: рабочая папка внутри смонтированного каталога проектов.
        pairs.sort(key=lambda pair: len(_norm(pair[0])), reverse=True)
        return cls(tuple(pairs))

    def to_local(self, path: str) -> Path:
        """Путь агента → локальный путь сервера."""
        for host, local in self.pairs:
            rest = _strip_prefix(path, host)
            if rest is not None:
                return Path(local, *rest)
        return Path(path)

    def to_host(self, path: Path | str) -> str:
        """Локальный путь сервера → путь агента (для ответов).

        Строка передаётся как есть: `Path` на Windows переписывает `/projects/…`
        в `\\projects\\…`, и префикс карты путей больше не совпадает.
        """
        text = path if isinstance(path, str) else str(path)
        for host, local in sorted(self.pairs, key=lambda pair: len(pair[1]), reverse=True):
            rest = _strip_prefix(text, local)
            if rest is not None:
                separator = "\\" if "\\" in host or ":" in host else "/"
                return separator.join([host.rstrip("\\/"), *rest])
        return text


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").casefold()


def _strip_prefix(path: str, prefix: str) -> list[str] | None:
    """Части пути после префикса или `None`, если путь не под ним (регистр не важен)."""
    norm_path, norm_prefix = _norm(path), _norm(prefix)
    if norm_path != norm_prefix and not norm_path.startswith(norm_prefix + "/"):
        return None
    tail = path.replace("\\", "/").rstrip("/")[len(norm_prefix) :]
    return [part for part in tail.split("/") if part]


def _is_absolute(path: str) -> bool:
    r"""Абсолютный путь Windows (`C:\…`, `\\сервер\…`) или POSIX (`/…`)."""
    return PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute()


@dataclass(slots=True)
class Settings:
    """Настройки сервера из окружения."""

    cache_dir: Path = Path("cache")
    workspace: Path = Path("workspace")
    path_map: PathMap = field(default_factory=PathMap)
    host: str = "127.0.0.1"
    port: int = 8060
    # Общий секрет MCP (`KD2_TOKEN`); пусто — заголовок Authorization не проверяется.
    token: str | None = None
    # Общий файл проектов и папки проектов на этой машине (или в контейнере).
    projects_file: Path = Path("projects.yaml")
    project_dirs: dict[str, Path] = field(default_factory=dict)
    # Папки живых правил проектов (`rules_dir`), доступные на запись; пусто — из projects.yaml.
    rules_dirs: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """`KD2_CACHE_DIR`, `KD2_WORKSPACE`, `KD2_PATH_MAP`, `KD2_HOST`, `KD2_PORT`, `KD2_TOKEN`,
        `KD2_PROJECTS_FILE`, `KD2_PROJECT_DIRS`, `KD2_RULES_DIRS`.

        Без `KD2_PROJECT_DIRS` папки проектов берутся из `projects.local.yaml` рядом с
        `projects.yaml` (локальный запуск без Docker); без `KD2_RULES_DIRS` папки живых правил —
        `rules_dir` проектов от этих папок. Пустой `KD2_TOKEN` — токена нет.
        """
        source = os.environ if env is None else env
        projects_file = Path(source.get("KD2_PROJECTS_FILE", "projects.yaml"))
        dirs_text = source.get("KD2_PROJECT_DIRS", "")
        if dirs_text:
            dirs = parse_project_dirs(dirs_text)
        else:
            local = projects_file.with_name("projects.local.yaml")
            dirs = load_local(local).project_dirs if local.is_file() else {}
        return cls(
            cache_dir=Path(source.get("KD2_CACHE_DIR", "cache")),
            workspace=Path(source.get("KD2_WORKSPACE", "workspace")),
            path_map=PathMap.parse(source.get("KD2_PATH_MAP", "")),
            host=source.get("KD2_HOST", "127.0.0.1"),
            port=int(source.get("KD2_PORT", "8060")),
            token=source.get("KD2_TOKEN", "").strip() or None,
            projects_file=projects_file,
            project_dirs=dirs,
            rules_dirs=parse_project_dirs(source.get("KD2_RULES_DIRS", ""), "KD2_RULES_DIRS"),
        )
