"""Инструкция проверки данных: ShowError обходится режимом загрузки, запросы нужны всегда."""

import re
from dataclasses import replace

from kd2_rules_mcp.authoring.ed.manager_render import ManagerKit, ManagerManifest
from kd2_rules_mcp.authoring.ed.manifest import sha256
from kd2_rules_mcp.ed import model as ed
from kd2_rules_mcp.ed.schema.profile import Applicability, ValidationProfile
from kd2_rules_mcp.ed.schema.resolver import property_type
from kd2_rules_mcp.validation.ed_structure_snapshot import StructureSnapshot, metadata_key


def key_data_instruction(
    document: ed.EdDocument, profile: ValidationProfile, snapshot: StructureSnapshot | None
) -> str:
    """Обязательный физический путь ключа и явно объявленный источник; fill_check не используется.

    Все оболочки должны быть обязательны. Чужие вложенные ссылки не раскрываются.
    ЗначениеЗаполнено вызывается в BSL, а не в языке запроса. Нулевые значения нужно оценить
    по смыслу: обязательность формата сама по себе не запрещает число 0 или Ложь.
    """
    if profile.schema is None or snapshot is None:
        return ""
    schema = profile.schema
    app = Applicability.build(document, profile)
    checks = set()
    for rule in document.pko:
        if app.evaluate(rule, "send") is not True:
            continue
        key, _ = metadata_key(rule.configuration_object.value)
        owner = snapshot.objects.get(key) if key else None
        typ, status = profile.owner_type(rule, "send", app)
        if (
            not owner
            or "." not in owner.type_name
            or "Ссылка" not in owner.type_name
            or status != "resolved"
            or not typ
        ):
            continue
        for prop in rule.properties:
            if not prop.configuration_property or app.evaluate(prop, "send", rule) is not True:
                continue
            resolved = profile.resolve(typ, prop.format_property)
            if resolved.status != "resolved" or not resolved.physical_paths:
                continue
            path = resolved.physical_paths[0]
            if not path or not path[0].local.casefold().startswith("ключевыесвойства"):
                continue
            current, required = typ, True
            for part in path:
                member = next((p for p in schema.inherited[current.id] if p.name == part), None)
                if not member or member.lower <= 0 or member.nillable:
                    required = False
                    break
                target = property_type(schema, member)
                if target:
                    current = target
            if not required or not owner.property(prop.configuration_property):
                continue
            field = prop.configuration_property
            if not re.fullmatch(
                r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*)*",
                field,
            ):
                continue
            checks.add((owner.kind + "." + owner.name, field))
    if not checks:
        return ""
    blocks = [
        "\n## Проверьте данные перед первым обменом\n\n"
        "Проверьте все объявленные источники обязательных ключевых свойств ссылочных типов. "
        "Даже проверка заполнения ShowError не гарантирует заполнения: обмен и загрузки "
        "могут записать объект с ОбменДанными.Загрузка = Истина. "
        "Пустой ключ в ссылке способен остановить выгрузку всех ссылающихся объектов. "
        "Запросы ниже только читают данные; нулевые числа и Ложь оцените по ограничениям "
        "и смыслу свойства, а не считайте автоматически ошибкой.\n"
    ]
    for object_name, field in sorted(checks):
        blocks.append(
            f"\n### {object_name}.{field}\n\n```bsl\n"
            "Запрос = Новый Запрос;\n"
            f'Запрос.Текст = "ВЫБРАТЬ Данные.Ссылка, Данные.{field} КАК ПроверяемоеЗначение\n'
            f'|ИЗ {object_name} КАК Данные";\n'
            "Выборка = Запрос.Выполнить().Выбрать();\n"
            "Пока Выборка.Следующий() Цикл\n"
            "    Если Не ЗначениеЗаполнено(Выборка.ПроверяемоеЗначение) Тогда\n"
            f'        Сообщить("Проверьте {field}: " + Строка(Выборка.Ссылка));\n'
            "    КонецЕсли;\nКонецЦикла;\n```\n"
        )
    return "".join(blocks)


def with_key_data_instruction(
    kit: ManagerKit, text: str, previous: ManagerManifest | None, previous_files: dict
) -> ManagerKit:
    """Дополнение сервиса входит в манифест и preview-хеш; повторная сборка стабильна."""
    if not text:
        return kit
    instruction = kit.instruction + text
    files = {**kit.files, "instruction.md": instruction.encode("utf-8")}
    manifest = replace(
        kit.manifest,
        file_hashes={**kit.manifest.file_hashes, "instruction.md": sha256(files["instruction.md"])},
    )
    status = "ready"
    if previous:
        repeated = replace(manifest, changes=previous.changes)
        if {**files, "manifest.json": repeated.to_bytes()} == previous_files:
            manifest, status = repeated, "unchanged"
    files["manifest.json"] = manifest.to_bytes()
    return replace(kit, files=files, instruction=instruction, manifest=manifest, status=status)
