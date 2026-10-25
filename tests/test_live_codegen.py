"""Проверяется текст каждого генератора, включая отсутствие записи в чтении."""

import base64
import hashlib

import pytest

from kd_rules_mcp.errors import LiveParseError
from kd_rules_mcp.live.codegen import exchange, files, plans, rules
from kd_rules_mcp.live.codegen.registration import registration_code
from kd_rules_mcp.live.response import literal
from kd_rules_mcp.live.runs import download_rule

VALUE = 'Имя"|;\r\nСимвол(10) Кириллица//текст'
FORBIDDEN = (
    "Записать",
    "Удалить",
    "СоздатьУзел",
    "ЗарегистрироватьИзменения",
    "УдалитьРегистрациюИзменений",
)


def snippets():
    return [
        (plans.plans_code(), True),
        (plans.bsp_plans_code(), True),
        (plans.overview_code(VALUE), False),
        (exchange.nodes_code(VALUE), True),
        (rules.metadata_code(VALUE, "exchange"), True),
        (rules.chunk_code(VALUE, "correspondent", 0, 4, VALUE), True),
        (rules.record_snapshot_code(VALUE, "exchange", VALUE), True),
        (rules.record_snapshot_code(VALUE, "registration", VALUE, offset=0, size=4), True),
        (rules.restore_file_code(VALUE, VALUE), False),
        (exchange.plan_content_check(VALUE, VALUE), True),
        (exchange.query_rows(VALUE), True),
        (rules.load_file_code(VALUE, VALUE, VALUE, write=False), True),
        (rules.load_file_code(VALUE, VALUE, VALUE, write=True), False),
        (exchange.this_node_code(VALUE, VALUE), False),
        (exchange.correspondent_node(VALUE, VALUE), False),
        (exchange.register_object(VALUE, VALUE, VALUE, VALUE), False),
        (exchange.export_registered(VALUE, VALUE), False),
        (exchange.export_object(VALUE, VALUE, VALUE, VALUE), False),
        (exchange.import_message(VALUE, VALUE, b"message"), False),
        (exchange.import_file(VALUE, VALUE, VALUE), False),
        (files.begin_file_code(), False),
        (files.append_chunk_code(VALUE, "YQ=="), False),
        (files.delete_file_code(VALUE), False),
        (files.file_info_code(VALUE), True),
        (registration_code(VALUE, VALUE, None, None, False), True),
        (
            registration_code(
                "Документ.Поступление",
                "11111111-1111-1111-1111-111111111111",
                "План",
                {"Имя": VALUE},
                True,
            ),
            False,
        ),
    ]


# Идентификатор — номер: текст генератора длиннее лимита имени теста Windows.
_SNIPPETS = snippets()


@pytest.mark.parametrize(
    "code,readonly", _SNIPPETS, ids=[str(index) for index in range(len(_SNIPPETS))]
)
def test_all_generators_single_line(code: str, readonly: bool) -> None:
    assert "\n" not in code and "\r" not in code and "//" not in code
    assert code.startswith("Попытка ") and code.endswith("КонецПопытки;")
    assert "ЗаписатьJSON(" in code
    if readonly:
        # Служебная сериализация JSON не является записью в базу.
        body = code.split(" Запись = Новый ЗаписьJSON;", 1)[0].replace("ЗаписатьJSON", "JSON")
        assert all(word not in body for word in FORBIDDEN)
    if "Кириллица" in code:
        assert literal(VALUE) in code


class Chunks:
    def __init__(self, replies):
        self.replies = replies

    def execute(self, code):
        assert "\n" not in code and "//" not in code
        return self.replies.pop(0)


def test_chunk_integrity() -> None:
    digest = base64.b64encode(hashlib.sha256(b"abc").digest()).decode()
    info = {"size": 3, "length": 4, "hash": digest}
    assert (
        download_rule(
            Chunks([f'OK {{"chunk":"YWJj","hash":"{digest}"}}']), "План", "exchange", info, 4
        )
        == b"abc"
    )


@pytest.mark.parametrize("part,digest", [("YQ", "same"), ("YQ==", "changed"), ("AAAA", "same")])
def test_chunk_short_hash_mismatch_and_changed_fingerprint(part: str, digest: str) -> None:
    import json

    info = {"size": 3, "length": 4, "hash": "same"}
    with pytest.raises(LiveParseError):
        download_rule(
            Chunks(["OK " + json.dumps({"chunk": part, "hash": digest})]),
            "План",
            "exchange",
            info,
            4,
        )
