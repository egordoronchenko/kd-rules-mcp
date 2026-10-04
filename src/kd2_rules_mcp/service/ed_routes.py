"""Снимки маршрутов EnterpriseData в памяти процесса и сравнение двух профилей.

Разбор выгрузки и загрузка схем идут вне общей блокировки. В реестре — не больше 32 снимков
и 128 МиБ оценки сохранённых текстов; старый идентификатор после вытеснения не находится.
Признак `stale` считается по каталогам, которые читает `read_routes`, а не по всей выгрузке.
"""

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import FunctionType
from typing import Any, cast

from kd2_rules_mcp.ed import routes as route_reader
from kd2_rules_mcp.ed.errors import EdFormatError, EdReadError, EdResourceLimitError
from kd2_rules_mcp.ed.model import EdDocument
from kd2_rules_mcp.ed.route_model import RouteProfile
from kd2_rules_mcp.ed.routes import read_routes
from kd2_rules_mcp.ed.schema import EdSchema, load_schema
from kd2_rules_mcp.errors import (
    EdRouteFormatError,
    EdRouteProfileNotFoundError,
    EdRouteReadError,
    EdRouteResourceLimitError,
    EdSchemaAmbiguousImportError,
    Kd2Error,
)
from kd2_rules_mcp.projects import ProjectConfigError, resolve
from kd2_rules_mcp.service import ed_routes_views as views
from kd2_rules_mcp.service.base import ServiceBase
from kd2_rules_mcp.service.ed_views import validate_page
from kd2_rules_mcp.service.paths import Settings
from kd2_rules_mcp.validation.ed_routes import (
    RouteSelection,
    RouteSelectionError,
    SchemaUnavailable,
    SideSelection,
    compare_routes,
    select_route,
)

MAX_PROFILES = 32
MAX_STORED_BYTES = 128 * 1024 * 1024


def _file_stamp(path: Path) -> tuple[int, int] | None:
    """Свежесть известного файла; отсутствие тоже проверяем без обхода каталога."""
    try:
        info = path.stat()
        return info.st_size, info.st_mtime_ns
    except FileNotFoundError:
        return None


# Что читает `read_routes`: описание конфигурации и четыре каталога. Обход всей выгрузки большой
# конфигурации (сотни тысяч файлов) занимает десятки секунд при каждом открытии.
_MANIFEST_FILES = ("Configuration.xml",)
_MANIFEST_DIRS = ("ExchangePlans", "CommonModules", "Subsystems", "XDTOPackages")


@dataclass(slots=True)
class _RouteSnapshot:
    """Один выданный снимок. Профиль не подменяется у уже выданного идентификатора."""

    profile: RouteProfile
    root: Path
    host_path: str
    project: str | None
    configuration: str | None
    manifest: tuple[tuple[str, int, int], ...] | None
    stored_bytes: int
    stale: bool = False
    read_files: dict[Path, tuple[int, int] | None] = field(default_factory=dict)
    file_hashes: dict[Path, str] = field(default_factory=dict)


class EdRoutesMixin(ServiceBase):
    """Два инструмента: чтение одного снимка и отчёт совместимости пары."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._routes: OrderedDict[str, _RouteSnapshot] = OrderedDict()
        self._route_roots: dict[str, str] = {}
        self._route_bytes = 0
        self._route_reads: dict[
            str, tuple[dict[Path, str], dict[Path, tuple[int, int] | None]]
        ] = {}

    def ed_routes(
        self,
        project: str | None = None,
        configuration: str = "full",
        path: str | None = None,
        profile_id: str | None = None,
        section: str = "summary",
        plan: str | None = None,
        offset: int = 0,
        limit: int = 50,
        force: bool = False,
    ) -> dict[str, Any]:
        """Один источник: проект, путь выгрузки или уже открытый снимок."""
        validate_page(offset, limit)
        _require_bool(force, "force")
        section_name = _choice(section, views.ROUTE_SECTIONS, "Раздел снимка")
        project_name = _optional_text(project, "project")
        path_text = _optional_text(path, "path")
        ident = _optional_text(profile_id, "profile_id")
        plan_text = _optional_text(plan, "plan")
        configuration_name = _required_text(configuration, "configuration")
        selected = sum(item is not None for item in (project_name, path_text, ident))
        if selected != 1:
            raise ValueError("Укажите ровно один источник: project, path или profile_id")
        if ident is not None and (force or configuration_name != "full"):
            raise ValueError("profile_id не сочетается с configuration и force")
        if path_text is not None and configuration_name != "full":
            raise ValueError("configuration применим только к project")

        if ident is not None:
            snap = self._require_route(ident)
            reused, stale = True, snap.stale
        else:
            if project_name is not None:
                root = self._project_root(project_name, configuration_name)
                bound_project: str | None = project_name
                bound_configuration: str | None = configuration_name
            else:
                root = self._visible_root(path_text or "")
                bound_project = None
                bound_configuration = None
            snap, reused, stale = self._open_snapshot(
                root, bound_project, bound_configuration, force
            )
        canonical = None if plan_text is None else _canonical_plan(snap.profile, plan_text)
        if section_name == "summary":
            return views.route_summary(snap.profile, _source_view(snap), reused=reused, stale=stale)
        rows = views.route_rows(snap.profile, section_name, canonical)
        return views.route_page(
            snap.profile.profile_id,
            section_name,
            rows,
            offset,
            limit,
            reused=reused,
            stale=stale,
        )

    def ed_route_compare(
        self,
        left_profile_id: str,
        right_profile_id: str,
        left_plan: str | None = None,
        right_plan: str | None = None,
        context: str = "plan",
        section: str = "issues",
        level: str | None = None,
        check_prefix: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Два уже открытых снимка. Ошибка схемы выбранного URI становится пропуском."""
        validate_page(offset, limit)
        section_name = _choice(section, views.COMPARE_SECTIONS, "Раздел отчёта")
        route_context = _choice(context, ("plan", "without_node"), "Контекст маршрута")
        level_name = _level(level)
        prefix = _prefix(check_prefix)
        left_name = _optional_text(left_plan, "left_plan")
        right_name = _optional_text(right_plan, "right_plan")
        if route_context == "without_node" and (left_name is not None or right_name is not None):
            raise ValueError("Для контекста без узла планы не задаются")
        left_id = _required_text(left_profile_id, "left_profile_id")
        right_id = _required_text(right_profile_id, "right_profile_id")
        left = self._require_route(left_id)
        right = self._require_route(right_id)
        try:
            selection = select_route(
                left.profile,
                right.profile,
                context=route_context,
                left_plan=left_name,
                right_plan=right_name,
            )
        except RouteSelectionError as error:
            raise ValueError(str(error)) from error
        schemas = self._schemas(left, right, selection)
        comparison = compare_routes(left.profile, right.profile, selection, schemas)
        selected = {
            "left": self._open_arguments(left, comparison.profile.left),
            "right": self._open_arguments(right, comparison.profile.right),
        }
        return views.compare_response(
            left_id=left_id,
            right_id=right_id,
            comparison=comparison,
            selected=selected,
            left=left.profile,
            right=right.profile,
            section=section_name,
            level=level_name,
            check_prefix=prefix,
            offset=offset,
            limit=limit,
        )

    def _project_root(self, project: str, configuration: str) -> Path:
        try:
            config = self._catalog().configuration(project, configuration)
        except ProjectConfigError as error:
            raise Kd2Error(str(error)) from error
        folder = self.settings.project_dirs.get(project)
        if folder is None:
            raise Kd2Error("Папка проекта не подключена")
        return self._visible_root(self._host(resolve(folder, config.dump)))

    def _visible_root(self, path: str) -> Path:
        try:
            return self._read_path(path).resolve()
        except Kd2Error as error:
            raise EdRouteReadError(str(error)) from error

    def _require_route(self, profile_id: str) -> _RouteSnapshot:
        with self._lock:
            snap = self._routes.get(profile_id)
            if snap is None:
                raise EdRouteProfileNotFoundError(
                    f"Снимок маршрутов «{profile_id}» не открыт или вытеснен"
                )
            self._routes.move_to_end(profile_id)
            return snap

    def _open_snapshot(
        self,
        root: Path,
        project: str | None,
        configuration: str | None,
        force: bool,
        *,
        documents: Mapping[Path, EdDocument] | None = None,
        read_files_only: bool = False,
        verify_read_files: bool = True,
    ) -> tuple[_RouteSnapshot, bool, bool]:
        key = str(root)
        if not force:
            with self._lock:
                snap = self._routes.get(self._route_roots.get(key, ""))
            if snap is not None:
                stale = (
                    snap.stale
                    if read_files_only and not verify_read_files
                    else (
                        any(_file_stamp(p) != stamp for p, stamp in snap.read_files.items())
                        if read_files_only or snap.manifest is None
                        else _is_stale(snap)
                    )
                )
                if not read_files_only and snap.manifest is None and not stale:
                    snap.manifest = _manifest(root)
                with self._lock:
                    current = self._routes.get(snap.profile.profile_id)
                    if current is not None:
                        current.stale = stale
                        self._routes.move_to_end(current.profile.profile_id)
                        return current, True, stale
        profile = self._read_profile(root, documents=documents)
        if project is not None:
            profile = replace(profile, project=project, configuration=configuration)
        manifest = None if read_files_only else _manifest(root) or ()
        file_hashes, read_files = self._route_reads.pop(str(root), ({}, {}))
        size = _stored_bytes(profile) + _stored_bytes(manifest) + _stored_bytes(file_hashes)
        host_path = self._host(root)
        with self._lock:
            current = self._routes.get(profile.profile_id)
            if current is not None:
                current.manifest = manifest
                current.read_files = read_files
                current.file_hashes = file_hashes
                current.stale = False
                self._routes.move_to_end(profile.profile_id)
                self._route_roots[key] = profile.profile_id
                return current, True, False
            self._make_room(size)
            created = _RouteSnapshot(
                profile,
                root,
                host_path,
                project,
                configuration,
                manifest,
                size,
                False,
                read_files,
                file_hashes,
            )
            self._routes[profile.profile_id] = created
            self._route_bytes += size
            self._route_roots[key] = profile.profile_id
            return created, False, False

    def _make_room(self, extra: int) -> None:
        if extra > MAX_STORED_BYTES:
            raise EdRouteResourceLimitError(
                "Снимок маршрутов превышает предел сохранённых данных (128 МиБ)"
            )
        while self._routes and (
            len(self._routes) >= MAX_PROFILES or self._route_bytes + extra > MAX_STORED_BYTES
        ):
            ident, old = self._routes.popitem(last=False)
            self._route_bytes -= old.stored_bytes
            self._route_roots = {
                path: stored for path, stored in self._route_roots.items() if stored != ident
            }

    def _read_profile(
        self, root: Path, *, documents: Mapping[Path, EdDocument] | None = None
    ) -> RouteProfile:
        try:
            # Локальные зависимости reader: открытый менеджер не читается и не разбирается вновь.
            # Функции маршрутов и их глобалы остаются неизменными для параллельных вызовов.
            original_manager = cast(Any, route_reader._Reader._manager)
            opened = documents or {}
            observed: dict[Path, tuple[int, int] | None] = {}

            def read_manager(path: Path) -> EdDocument:
                return opened[path] if path in opened else route_reader.read_manager(path)

            manager = FunctionType(
                original_manager.__code__,
                {**original_manager.__globals__, "read_manager": read_manager},
            )
            readers = []

            def metadata(path: Path):
                observed.setdefault(path, _file_stamp(path))
                return route_reader.package_metadata(path)

            original_packages = cast(Any, route_reader._Reader._read_packages)
            packages = FunctionType(
                original_packages.__code__,
                {**original_packages.__globals__, "package_metadata": metadata},
            )

            class CachedReader(route_reader._Reader):
                def __init__(self, path: Path):
                    super().__init__(path)
                    readers.append(self)

                def _manager(self, name: str):
                    canonical = self.inventory.module_names.get(name.casefold(), name)
                    if name.casefold() in self.inventory.module_names:
                        path = self.root / "CommonModules" / canonical / "Ext/Module.bsl"
                        observed.setdefault(path, _file_stamp(path))
                    return manager(self, name)

                def _xml(self, path: Path):
                    observed.setdefault(path, _file_stamp(path))
                    return super()._xml(path)

                def _load_bsl(self, path: Path):
                    observed.setdefault(path, _file_stamp(path))
                    return super()._load_bsl(path)

                def _read_packages(self):
                    # Отсутствие известного файла участвует в выборе маршрута/импорта.
                    for name in self.inventory.packages:
                        for relative in (
                            f"XDTOPackages/{name}.xml",
                            f"XDTOPackages/{name}/Ext/Package.bin",
                        ):
                            path = self.root / relative
                            if _file_stamp(path) is None:
                                observed[path] = None
                    return packages(self)

                def _read_plan(self, name: str):
                    for relative in (
                        f"ExchangePlans/{name}.xml",
                        f"ExchangePlans/{name}/Ext/ManagerModule.bsl",
                    ):
                        path = self.root / relative
                        if _file_stamp(path) is None:
                            observed[path] = None
                    return super()._read_plan(name)

            original = cast(Any, read_routes)
            read = FunctionType(
                original.__code__, {**original.__globals__, "_Reader": CachedReader}
            )
            profile = read(root)
            hashes = {
                root / name: fingerprint
                for reader in readers
                for name, fingerprint in reader.files.items()
            }
            self._route_reads[str(root)] = (hashes, observed)
            return profile
        except EdResourceLimitError as error:
            raise EdRouteResourceLimitError(str(error)) from error
        except EdFormatError as error:
            raise EdRouteFormatError(str(error)) from error
        except EdReadError as error:
            raise EdRouteReadError(str(error)) from error
        except OSError as error:
            raise EdRouteReadError("Выгрузка маршрутов недоступна") from error

    def _schemas(
        self,
        left: _RouteSnapshot,
        right: _RouteSnapshot,
        selection: RouteSelection,
    ) -> dict[tuple[str, str], EdSchema | SchemaUnavailable]:
        needed = dict(selection.required_uris)
        loaded: dict[tuple[str, str], EdSchema | SchemaUnavailable] = {}
        for side, snap in (("left", left), ("right", right)):
            for uri in needed.get(side, ()):
                loaded[(side, uri)] = _load_for_uri(snap, uri)
        return loaded

    def _open_arguments(self, snap: _RouteSnapshot, side: SideSelection) -> dict[str, Any]:
        """Аргументы перехода. Нет однозначного пути или пакета — null и причина."""
        ed_open = None
        ed_open_reason = None
        if side.manager_path:
            ed_open = {"path": self._host((snap.root / side.manager_path).resolve())}
        elif side.manager_name:
            ed_open_reason = "Тело модуля менеджера не прочитано"
        else:
            ed_open_reason = "Менеджер выбранного маршрута не определён"

        schema: dict[str, Any] | None = None
        schema_reason = side.schema_reason
        if (
            schema_reason is None
            and side.format_version
            and side.package_metadata_name
            and side.package_path
        ):
            imports: dict[str, str] = {}
            for uri, raw in side.schema_imports:
                if not raw:
                    schema_reason = "Импорт выбранной схемы разрешён не однозначно"
                    break
                imports[uri] = self._host(Path(raw).resolve())
            else:
                if snap.project and snap.configuration:
                    schema = {
                        "format_version": side.format_version,
                        "project": snap.project,
                        "configuration": snap.configuration,
                        "package": side.package_metadata_name,
                    }
                else:
                    schema = {
                        "format_version": side.format_version,
                        "path": self._host((snap.root / side.package_path).resolve()),
                    }
                if imports:
                    schema["imports"] = imports
        elif schema_reason is None:
            schema_reason = (
                "Маршрут не выбран"
                if side.format_version is None
                else "Пакет выбранной версии не определён однозначно"
            )
        return {
            "manager_name": side.manager_name,
            "ed_open": ed_open,
            "ed_open_reason": ed_open_reason,
            "ed_schema_open": schema,
            "ed_schema_reason": schema_reason,
        }


def _load_for_uri(snap: _RouteSnapshot, uri: str) -> EdSchema | SchemaUnavailable:
    """Схема попадает в сравнение только под запрошенным URI. Любой сбой чтения — пропуск."""
    matches = [item for item in snap.profile.packages if item.namespace == uri]
    if not matches:
        return SchemaUnavailable("missing", f"Пакет пространства «{uri}» не найден", uri)
    if len(matches) > 1:
        places = ", ".join(item.description_path for item in matches[:3])
        return SchemaUnavailable(
            "ambiguous",
            f"Неоднозначный пакет пространства «{uri}»: {places}",
            places,
        )
    package = matches[0]
    relative = package.package_path or package.description_path
    place = relative
    try:
        schema = load_schema(snap.root / relative, locate_import=_locate(snap))
    except Exception as error:
        # Кривой пакет, пустой файл, лимит и любая другая ошибка чтения не роняют сравнение.
        return SchemaUnavailable("unreadable", f"{error} ({place})", place)
    if schema.base_namespace != uri:
        return SchemaUnavailable(
            "unreadable",
            f"Схема «{schema.base_namespace}» не соответствует пространству «{uri}» ({place})",
            place,
        )
    return schema


def _locate(snap: _RouteSnapshot) -> Callable[[str], Path | None]:
    groups: dict[str, list[Path]] = {}
    for item in snap.profile.packages:
        if item.package_path:
            groups.setdefault(item.namespace, []).append(snap.root / item.package_path)

    def locate(uri: str) -> Path | None:
        found = groups.get(uri, [])
        if len(found) > 1:
            raise EdSchemaAmbiguousImportError(f"Неоднозначный пакет пространства «{uri}»")
        return found[0] if found else None

    return locate


def _source_view(snap: _RouteSnapshot) -> dict[str, Any]:
    source: dict[str, Any] = {
        "kind": "project" if snap.project else "path",
        "path": snap.host_path,
        "fingerprint": snap.profile.sources_fingerprint,
    }
    if snap.project is not None:
        source["project"] = snap.project
        source["configuration"] = snap.configuration
    return source


def _canonical_plan(profile: RouteProfile, name: str) -> str:
    """Имя плана без учёта регистра. В ответе остаётся написание из конфигурации."""
    folded = name.casefold()
    found = [plan.plan_name for plan in profile.plans if plan.plan_name.casefold() == folded]
    if not found:
        raise ValueError(f"План обмена «{name}» не найден")
    if len(found) > 1:
        raise ValueError(f"Несколько планов обмена с именем «{name}»: {', '.join(found)}")
    return found[0]


def _manifest(root: Path) -> tuple[tuple[str, int, int], ...] | None:
    """Имена, размеры и время файлов, которые читает `read_routes`.

    Описание конфигурации и каталоги планов обмена, общих модулей, подсистем и пакетов XDTO;
    новый файл в них тоже делает снимок устаревшим. Остальная выгрузка на маршруты не влияет.
    """
    rows: list[tuple[str, int, int]] = []
    try:
        files = [root / name for name in _MANIFEST_FILES]
        for name in _MANIFEST_DIRS:
            folder = root / name
            if folder.is_dir():
                files.extend(folder.rglob("*"))
        for path in files:
            if not path.is_file():
                continue
            stat = path.stat()
            rows.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
    except OSError:
        return None
    rows.sort()
    return tuple(rows)


def _is_stale(snap: _RouteSnapshot) -> bool:
    current = _manifest(snap.root)
    return current is None or current != snap.manifest


def _stored_bytes(value: object, seen: set[int] | None = None) -> int:
    """Оценка текстов снимка. Один объект считается один раз."""
    if seen is None:
        seen = set()
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if value is None or isinstance(value, (int, float, bool)):
        return 8
    marker = id(value)
    if marker in seen:
        return 0
    seen.add(marker)
    if isinstance(value, (tuple, list)):
        return sum(_stored_bytes(item, seen) for item in value)
    if isinstance(value, dict):
        return sum(
            _stored_bytes(key, seen) + _stored_bytes(item, seen) for key, item in value.items()
        )
    fields = getattr(value, "__dataclass_fields__", None)
    if isinstance(fields, dict):
        return sum(_stored_bytes(getattr(value, name), seen) for name in fields)
    return 0


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} должен быть строкой")
    text = value.strip()
    return text or None


def _required_text(value: object, name: str) -> str:
    text = _optional_text(value, name)
    if text is None:
        raise ValueError(f"Нужно непустое значение {name}")
    return text


def _require_bool(value: object, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} должен быть логическим значением")


def _choice(value: object, allowed: tuple[str, ...], title: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        names = ", ".join(allowed)
        raise ValueError(f"{title}: {names}")
    return value


def _level(value: object) -> str | None:
    if value is None:
        return None
    if value not in ("ошибка", "предупреждение"):
        raise ValueError("Уровень: «ошибка» или «предупреждение»")
    return str(value)


def _prefix(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("check_prefix должен быть строкой")
    text = value.strip()
    return text or None
