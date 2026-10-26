"""Проекты правил, списки и точечные правки."""

from collections.abc import Mapping
from typing import Any

from kd_rules_mcp.authoring.edits import (
    CONVERSION_KIND,
    create_pko_many,
    create_pko_with_properties,
    create_rule,
    delete_rule,
    find_rule,
    update_rule,
    update_rules,
)
from kd_rules_mcp.authoring.pack import collect, pack_rules
from kd_rules_mcp.authoring.workspace import RulesProject
from kd_rules_mcp.errors import Kd2Error, RulesSaveErrorsError
from kd_rules_mcp.kd2.diff import SECTIONS, RuleChange, diff_rules, ignored_header_fields
from kd_rules_mcp.kd2.model import ExchangeRules, RegistrationRules, RulesDocument
from kd_rules_mcp.kd2.rules_io import load_rules
from kd_rules_mcp.service.base import ServiceBase
from kd_rules_mcp.service.checks import validation_report
from kd_rules_mcp.service.views import (
    PKS_SECTION,
    REGISTRATION_SECTION,
    batch_update_view,
    conversion_view,
    counts,
    find_pro,
    listed_rule_rows,
    node_view,
    note_private,
    overview_groups,
    page_limit,
    parse_detail,
    pko_batch_view,
    pro_view,
    report_summary,
    rule_group,
    slice_rows,
)
from kd_rules_mcp.validation.report import ValidationReport

# Столько ошибок проверки попадает в отказ и в ответ записи с подтверждением.
_SAVE_ERRORS_LIMIT = 50


def _used_structures(
    source_id: str | None,
    target_id: str | None,
    exchange_id: str | None,
) -> dict[str, str | None]:
    """Идентификаторы, которые проверка открыла. Пустая строка — стороны не было.

    `exchange_project` — парный проект комплекта: правила обмена у правил регистрации
    и правила регистрации у правил обмена.
    """
    return {
        "source": source_id or None,
        "target": target_id or None,
        "exchange_project": exchange_id or None,
    }


def _validation_view(
    report: ValidationReport,
    structures: dict[str, str | None],
    dropped: list[str],
) -> dict[str, Any]:
    """Итог `report_summary` плюс фактически открытые структуры и отброшенные идентификаторы."""
    view = report_summary(report)
    view["structures"] = structures
    if dropped:
        view["dropped"] = dropped
    return view


class RulesMixin(ServiceBase):
    """Открытие проектов правил, чтение разделов и правки по адресу."""

    def rules_open(self, path: str, private: bool = False) -> dict[str, Any]:
        with self._lock:
            opened = self.workspace.open_rules(self._read_path(path), private=private)
            view = self._project_view(opened.project)
            view["reused"] = opened.reused
            if opened.reused:
                view["source_changed"] = opened.source_changed
            return note_private(view, opened.project.private)

    def rules_create(
        self, source_structure: str, target_structure: str, project_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            project = self.workspace.create_exchange(
                self.store, source_structure, target_structure, project_id
            )
            return self._project_view(project)

    def rules_projects(self) -> dict[str, Any]:
        with self._lock:
            return {
                "projects": [
                    note_private(self._project_view(item), item.private)
                    for item in self.workspace.iter_projects()
                ]
            }

    def rules_close(self, project_id: str) -> dict[str, Any]:
        """Удаляет рабочий проект и его снимок. Файл `rules_save` не трогает."""
        with self._lock:
            removed = self.workspace.close(project_id)
            return {"project_id": project_id, "closed": True, "snapshot_removed": removed}

    def rules_overview(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            project = self.workspace.get(project_id)
            view = self._project_view(project)
            document = project.document
            if document is not None:
                groups = overview_groups(document)
                if groups:
                    view["groups"] = groups
            return view

    def rules_list(
        self, project_id: str, section: str, text: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        with self._lock:
            rows = listed_rule_rows(self._document(project_id), section)
        if text:
            needle = text.casefold()
            if section == PKS_SECTION:
                rows = [
                    row
                    for row in rows
                    if needle in str(row.get("source", "")).casefold()
                    or needle in str(row.get("target", "")).casefold()
                ]
            else:
                rows = [
                    row for row in rows if needle in " ".join(map(str, row.values())).casefold()
                ]
        return slice_rows(rows, offset, limit)

    def rules_get(
        self, project_id: str, kind: str, key: str, owner: str, limit: int
    ) -> dict[str, Any]:
        with self._lock:
            document = self._document(project_id)
            if isinstance(document, RegistrationRules):
                page_limit(limit)
                if kind != "pro":
                    raise Kd2Error(
                        f"Проект «{project_id}» — правила регистрации. "
                        f"Доступен вид pro, раздел «{REGISTRATION_SECTION}»"
                    )
                node, address, group = find_pro(document, key)
                return pro_view(node, address, group)
            if not isinstance(document, ExchangeRules):
                raise Kd2Error(f"Проект «{project_id}» не загружен")
            if kind == CONVERSION_KIND:
                page_limit(limit)
                find_rule(document, kind, key, owner)
                return conversion_view(document)
            node = find_rule(document, kind, key, owner)
            return node_view(node, page_limit(limit), rule_group(document, node))

    def rules_save(
        self,
        project_id: str,
        path: str,
        overwrite: bool,
        acknowledge_errors: bool = False,
        source_structure: str | None = None,
        target_structure: str | None = None,
        exchange_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Проверяет правила как `rules_validate` и пишет XML. Ошибки без подтверждения — отказ.

        Сначала путь и `overwrite`, затем проверка. Запись с ошибками — только при
        `acknowledge_errors`. Без явной тройки структур берётся последняя успешная
        `rules_validate` этого проекта; пропавшие из кэша отбрасываются.
        """
        with self._lock:
            allowed = list(self.rules_dirs().values())
            local = self._writable(path)
            self.workspace.check_destination(local, overwrite=overwrite, allowed=allowed)
            document = self._document(project_id)
            project = self.workspace.get(project_id)
            source_id, target_id, exchange_id, dropped = self._validation_inputs(
                project, source_structure, target_structure, exchange_project_id
            )
            with self._sides(source_id, target_id) as (source, target):
                paired = self._paired(document, exchange_id, project.source_path)
                report = validation_report(document, source, target, project.source_path, paired)
            validation = _validation_view(
                report,
                _used_structures(source_id, target_id, exchange_id),
                dropped,
            )
            error_items = [issue.to_dict() for issue in report.errors]
            if error_items and not acknowledge_errors:
                raise RulesSaveErrorsError(
                    f"Правила не записаны: ошибок проверки {len(error_items)}. "
                    "Исправьте их или повторите с acknowledge_errors=true",
                    {
                        "validation": validation,
                        "errors": error_items[:_SAVE_ERRORS_LIMIT],
                        "errors_total": len(error_items),
                    },
                )
            saved = self.workspace.save(project_id, local, overwrite=overwrite, allowed=allowed)
            result: dict[str, Any] = {
                "project_id": project_id,
                "path": self._host(saved),
                "size_bytes": saved.stat().st_size,
                "counts": counts(document),
                "validation": validation,
                "saved_with_errors": bool(error_items),
            }
            if error_items:
                result["errors"] = error_items[:_SAVE_ERRORS_LIMIT]
                result["errors_total"] = len(error_items)
            return result

    def _validation_inputs(
        self,
        project: RulesProject,
        source_structure: str | None,
        target_structure: str | None,
        exchange_project_id: str | None,
    ) -> tuple[str | None, str | None, str | None, list[str]]:
        """Явная тройка целиком, если передан хотя бы один аргумент; иначе запомненная.

        Пустой идентификатор — сторона не задана. Запомненная структура, которой нет в кэше,
        и проект обмена, которого уже нет среди открытых, отбрасываются. Явно переданная
        неизвестная структура здесь не проверяется: её откроет `_sides`, как в `rules_validate`.
        """
        if (
            source_structure is not None
            or target_structure is not None
            or exchange_project_id is not None
        ):
            return source_structure, target_structure, exchange_project_id, []
        remembered = project.validation_inputs
        if remembered is None:
            return None, None, None, []
        source, target, exchange = remembered
        dropped: list[str] = []
        if source and not self.store.exists(source):
            dropped.append(f"source:{source}")
            source = None
        if target and not self.store.exists(target):
            dropped.append(f"target:{target}")
            target = None
        if exchange and exchange not in self.workspace.ids():
            dropped.append(f"exchange_project:{exchange}")
            exchange = None
        return source, target, exchange, dropped

    def rules_pack(
        self,
        folder: str,
        exchange_rules: str,
        correspondent_rules: str,
        registration_rules: str,
        path: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        with self._lock:
            source_dir = self._read_path(folder) if folder else None
            explicit = {
                role: self._read_path(value) if value else None
                for role, value in (
                    ("exchange", exchange_rules),
                    ("correspondent", correspondent_rules),
                    ("registration", registration_rules),
                )
            }
            files = collect(source_dir, explicit)
            default = source_dir.name if source_dir is not None else files["exchange"].stem
            result = pack_rules(
                files, self._writable(path or f"{default}.zip"), overwrite=overwrite
            )
            return {
                "path": self._host(result.path),
                "size_bytes": result.path.stat().st_size,
                "sha256": result.sha256,
                "load_with": result.form,
                "files": [
                    {
                        "file": item.name,
                        "path": self._host(item.source),
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                        "rules": item.summary,
                    }
                    for item in result.files
                ],
                "warnings": result.warnings,
            }

    def rule_create(
        self, project_id: str, kind: str, key: str, group: str = "", **options: Any
    ) -> dict[str, Any]:
        return self._edit(create_rule, project_id, kind, key, group=group, **options)

    def rule_update(self, project_id: str, kind: str, key: str, **options: Any) -> dict[str, Any]:
        return self._edit(update_rule, project_id, kind, key, **options)

    def rule_update_many(
        self,
        project_id: str,
        kind: str,
        fields: Mapping[str, Any],
        owner: str = "",
        keys: list[str] | None = None,
        except_keys: list[str] | None = None,
        filter: dict[str, Any] | None = None,
        mode: str = "apply",
        expected_count: int | None = None,
        source_structure: str | None = None,
        target_structure: str | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Одни поля у нескольких правил одного вида. Отказ не помечает проект изменённым.

        `preview` выполняет ту же правку и возвращает документ. Проект меняется только в `apply`.
        """
        if mode not in ("apply", "preview"):
            raise ValueError(f"Режим «{mode}» не принимается. Допустимые: apply, preview")
        if not fields:
            raise Kd2Error("Не переданы поля для изменения")
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            rules = self._exchange(project_id)
            batch = update_rules(
                rules,
                kind,
                fields,
                owner=owner,
                keys=keys,
                except_keys=() if except_keys is None else except_keys,
                source=source,
                target=target,
                filter=filter,
                preview=mode == "preview",
                expected_count=expected_count,
            )
            if mode == "apply":
                self.workspace.mark_modified(project_id)
            return batch_update_view(kind, owner, mode, batch, offset, limit)

    def pko_create_many(
        self,
        project_id: str,
        source_structure: str,
        target_structure: str,
        kind: str | None = None,
        pairs: list[dict[str, Any]] | None = None,
        fields: Mapping[str, Any] | None = None,
        group: str = "",
        mode: str = "preview",
        expected_preview_hash: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """ПКО и ПКС по многим парам. Проект меняется только после apply с хешем предпросмотра."""
        with (
            self._lock,
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            rules = self._exchange(project_id)
            batch = create_pko_many(
                rules,
                source_structure,
                target_structure,
                source,
                target,
                kind=kind,
                pairs=pairs,
                fields=fields,
                group=group,
                mode=mode,
                expected_preview_hash=expected_preview_hash,
            )
            if mode == "apply":
                self.workspace.mark_modified(project_id)
            return pko_batch_view(batch, offset, limit)

    def rule_delete(
        self,
        project_id: str,
        kind: str,
        key: str,
        owner: str,
        source_structure: str | None,
        target_structure: str | None,
    ) -> dict[str, Any]:
        with self._lock, self._sides(source_structure, target_structure) as (source, target):
            rules = self._exchange(project_id)
            result = delete_rule(rules, kind, key, owner=owner, source=source, target=target)
            return self._edited(project_id, result)

    def pko_create_from_candidates(
        self,
        project_id: str,
        code: str,
        source_structure: str,
        target_structure: str,
        source_object: str,
        target_object: str,
        fields: Mapping[str, Any] | None,
        group: str = "",
    ) -> dict[str, Any]:
        with (
            self._lock,
            self._structure(source_structure) as source,
            self._structure(target_structure) as target,
        ):
            rules = self._exchange(project_id)
            result = create_pko_with_properties(
                rules, code, source, target, source_object, target_object, fields, group=group
            )
            return self._edited(project_id, result)

    def rules_diff(
        self,
        left: str,
        right: str,
        include_header: bool,
        order: bool,
        section: str | None,
        offset: int,
        limit: int,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Смысловой дифф двух сторон: открытый проект или файл правил по пути агента."""
        if section and section not in SECTIONS:
            known = ", ".join(SECTIONS)
            raise Kd2Error(f"Неизвестный раздел «{section}»; разделы: {known}")
        mode = parse_detail(detail)
        left_ref, left_doc = self._diff_side(left)
        right_ref, right_doc = self._diff_side(right)
        changes = diff_rules(left_doc, right_doc, include_header=include_header, order=order)
        if section:
            changes = [item for item in changes if item.section == section]
        rows = _brief_rows(changes) if mode == "brief" else [_change_row(item) for item in changes]
        return {
            "left": left_ref,
            "right": right_ref,
            "kind": "registration" if left_doc.root_tag == "ПравилаРегистрации" else "exchange",
            "detail": mode,
            "ignored_fields": ignored_header_fields(include_header),
            "summary": _diff_summary(changes),
            "changes": slice_rows(rows, offset, limit),
        }

    def _diff_side(self, ref: str) -> tuple[dict[str, str], RulesDocument]:
        """Проект — по наличию идентификатора среди открытых; иначе путь, как у `rules_open`."""
        with self._lock:
            if ref in self.workspace.ids():
                return {"project_id": ref}, self._document(ref)
        path = self._read_path(ref)
        return {"path": self._host(path)}, load_rules(path)


def _diff_summary(changes: list[RuleChange]) -> dict[str, dict[str, int]]:
    """Счётчики по непустым разделам, в порядке разделов инструментов."""
    counts: dict[str, dict[str, int]] = {}
    for change in changes:
        bucket = counts.setdefault(change.section, {"added": 0, "removed": 0, "changed": 0})
        if change.change == "moved":
            bucket["moved"] = bucket.get("moved", 0) + 1
        else:
            bucket[change.change] += 1
    return {name: counts[name] for name in SECTIONS if name in counts}


def _brief_rows(changes: list[RuleChange]) -> list[dict[str, Any]]:
    """Адреса без содержимого. Повтор того же вида изменения по адресу схлопывается."""
    seen: set[tuple[str, str, str]] = set()
    rows: list[dict[str, Any]] = []
    for change in changes:
        key = (change.section, change.address, change.change)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"section": change.section, "address": change.address, "change": change.change})
    return rows


def _change_row(change: RuleChange) -> dict[str, Any]:
    row: dict[str, Any] = {
        "section": change.section,
        "address": change.address,
        "change": change.change,
    }
    if change.field is not None:
        row["field"] = change.field
    if change.old is not None:
        row["old"] = change.old
    if change.new is not None:
        row["new"] = change.new
    if change.handler_diff is not None:
        row["handler_diff"] = change.handler_diff
    if change.size is not None:
        row["size"] = change.size
    if change.preview:
        row["preview"] = change.preview
    return row
