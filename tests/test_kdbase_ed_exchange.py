"""`kdbase/ed_exchange_check.py` без баз 1С: стадии, случай, журнал, экранирование."""

import base64
import json
import re
import sys
from collections.abc import Callable
from html import escape
from pathlib import Path

import pytest
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kdbase"))

import ed_exchange_check as ed  # noqa: E402 — скрипт из kdbase/, не пакет

EXAMPLE = ROOT / "kdbase" / "ed_exchange_case.example.json"
SRC = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TGT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REF = "cccccccc-cccc-cccc-cccc-cccccccccccc"
STRANGER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
SECRET = "s3cret-password"
TOKEN = "c2VjcmV0"
META = "Справочник.ПримерОбъекты"
FORMAT = "http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.20"
MSG = "http://www.1c.ru/SSL/Exchange/Message"
PLAN = "ОбменПример"

NEW_CALL = re.compile(r"Новый \w+\([^;]*?\)\s*\.")


class _Box:
    """Состояние подставного сервера: экземпляр `DataServer` заморожен."""

    role: str
    this_code: str
    correspondent_code: str
    has_correspondent: bool
    has_object: bool
    ref: str
    value: str
    name_count: int
    name_ref: str
    registered: bool
    foreign: int
    sent: int
    received: int
    safe: bool
    hook_silent: bool
    fail_stage: str
    drop_stage: str
    drop_left: int
    apply_on_drop: bool
    freeze_number: bool
    force_property: bool
    absent_policy: str
    reject_import: bool
    lie_value: str | None
    hide_object: bool
    sync: str
    auto_register: bool
    fail_delete: bool
    files: dict[str, bytes]
    codes: list[str]
    values: list[str]
    created_name: str
    seen_node: str
    export_n: int
    import_n: int
    object_name: str
    node_name: str
    warn_import: bool
    cut_marker: str
    size_bias: int
    manager_other: bool
    leak: bool
    pko_dup: int
    tables: set[str] | None


def _box(**flags: object) -> _Box:
    box = _Box()
    box.role = "source"
    box.this_code = TGT
    box.correspondent_code = SRC
    box.has_correspondent = False
    box.has_object = False
    box.ref = ""
    box.value = ""
    box.name_count = 0
    box.name_ref = ""
    box.registered = False
    box.foreign = 0
    box.sent = 0
    box.received = 0
    box.safe = False
    box.hook_silent = False
    box.fail_stage = ""
    box.drop_stage = ""
    box.drop_left = 0
    box.apply_on_drop = False
    box.freeze_number = False
    box.force_property = False
    box.absent_policy = "keep"
    box.reject_import = False
    box.lie_value = None
    box.hide_object = False
    box.sync = "Да"
    box.auto_register = False
    box.fail_delete = False
    box.files = {}
    box.codes = []
    box.values = []
    box.created_name = ""
    box.seen_node = ""
    box.export_n = 0
    box.import_n = 0
    box.object_name = ""
    box.node_name = ed.NODE_NAME
    box.warn_import = False
    box.cut_marker = ""
    box.size_bias = 0
    box.manager_other = False
    box.leak = False
    box.pko_dup = 0
    box.tables = None
    for key, value in flags.items():
        setattr(box, key, value)
    return box


class Hub(ed.DataServer):
    """Ответы стадий по форме стенда: снимок, заполнитель, сообщение, приёмник."""

    box: _Box

    def __init__(self, label: str, box: _Box) -> None:
        super().__init__(
            label,
            f"http://user:{SECRET}@127.0.0.1/mcp",
            {"Authorization": f"Basic {TOKEN}"},
        )
        object.__setattr__(self, "box", box)

    def call(self, code: str) -> str:
        box: _Box = self.box
        box.codes.append(code)
        stage = _stage(code)
        forbidden = _forbidden_table(code, _allowed_tables(box))
        if forbidden:
            return _table_miss(forbidden)
        if box.fail_delete and stage == "delete-object":
            return "ОШИБКА не удалился"
        if box.fail_stage and stage == box.fail_stage:
            return f"ОШИБКА сбой стадии {stage}"
        if box.drop_stage and stage == box.drop_stage and box.drop_left:
            box.drop_left -= 1
            if box.apply_on_drop:
                self._apply(stage, code)
            return ""
        handler = {
            "state": self._state,
            "create-node": self._create_node,
            "create-object": self._create_object,
            "hook": self._hook,
            "write": self._write,
            "register": self._register,
            "unregister": self._unregister,
            "changes": self._changes,
            "export": self._export,
            "read-chunk": self._read_chunk,
            "delete-file": self._delete_file,
            "begin-file": self._begin_file,
            "append-chunk": self._append,
            "import": self._import,
            "query": self._query,
            "delete-object": self._delete_object,
            "delete-node": self._delete_node,
            "file-size": self._file_size,
            "registrations": self._registrations,
        }.get(stage)
        if handler is None:
            return f"ОШИБКА неизвестная стадия {stage}"
        result = handler(code)
        if box.cut_marker == stage and result.startswith("OK"):
            return "\n".join(line for line in result.splitlines() if line != ed.ANSWER_END)
        return result

    def _apply(self, stage: str, code: str) -> None:
        if stage == "write":
            self._take_value(code)
        elif stage == "import":
            self._consume(_file_of(code, "ИмяФайла"))
        elif stage == "create-node":
            self._open_node(code)
        elif stage == "create-object":
            self._open_object(code)

    def _state(self, code: str) -> str:
        box = self.box
        safe = "Да" if box.safe else "Нет"
        lines = [
            "КОНФИГУРАЦИЯ\tПример\t1.0.0.1",
            "ПЛАТФОРМА\t8.3.27.100",
            f"КОНСТ\tИспользоватьСинхронизациюДанных\t{box.sync}",
            f"РАСШ\tПримерРасширение\tДа\t{safe}",
            f"УЗЕЛ\t{box.this_code}\t1.20\t1\t0\t0\t1",
        ]
        if box.has_correspondent:
            lines.append(f"УЗЕЛ\t{box.correspondent_code}\t1.20\t0\t{box.sent}\t{box.received}\t1")
        lines.append("МЕНЕДЖЕР\tМенеджерОбменаПример\t2")
        asked = re.search(r'УникальныйИдентификатор\("([^"]+)"\)', code)
        if asked:
            if box.has_object and asked.group(1) == box.ref:
                lines.append(f"ОБЪЕКТ\tесть\t{box.ref}\t{ed.show(box.value)}")
                lines.append(f"ИМЯОБЪЕКТА\t{ed.show(box.object_name)}")
            else:
                lines.append("ОБЪЕКТ\tнет")
        if "ЗапросИмени" in code:
            lines.append(f"ИМЕНА\t{box.name_count}")
            if box.name_count == 1 and box.name_ref:
                lines.append(f"ИМЯУИД\t{box.name_ref}")
        return _ok(lines)

    def _open_node(self, code: str) -> None:
        box = self.box
        if "МенеджерКонстанты.Значение = Истина" in code:
            box.sync = "Да"
        found = re.search(r'НайтиПоКоду\("([^"]+)"\)', code)
        box.seen_node = found.group(1) if found else ""
        box.has_correspondent = True

    def _create_node(self, code: str) -> str:
        self._open_node(code)
        return _ok(["СОЗДАН\tда", f"КОД\t{self.box.seen_node}"])

    def _open_object(self, code: str) -> None:
        box = self.box
        found = re.search(r"ОбъектДанных\.Наименование = (.*?); ОбъектДанных\.Записать", code)
        box.created_name = eval_bsl(found.group(1)) if found else ""
        if box.name_count:
            return
        box.has_object = True
        box.ref = REF
        box.name_count = 1
        box.name_ref = REF
        box.object_name = box.created_name
        box.value = ""

    def _create_object(self, code: str) -> str:
        occupied = self.box.name_count > 0
        self._open_object(code)
        if occupied:
            return _ok(["СОЗДАН\tзанято", "СТРОК\t1"])
        return _ok(["СОЗДАН\tда", f"УИД\t{REF}"])

    def _hook(self, code: str) -> str:
        box = self.box
        pair = "0" if box.hook_silent else "1"
        if box.role == "source":
            opposite = "есть\t1\t1" if box.leak else "нет\t0\t0"
            rows = [
                "ВЕРСИЯ\t2",
                f"НАПРАВЛЕНИЕ\tОтправка\tесть\t{pair}\t{pair}",
                f"НАПРАВЛЕНИЕ\tПолучение\t{opposite}",
            ]
        else:
            opposite = "есть\t1\t1" if box.leak else "нет\t0\t0"
            rows = [
                "ВЕРСИЯ\t2",
                f"НАПРАВЛЕНИЕ\tОтправка\t{opposite}",
                f"НАПРАВЛЕНИЕ\tПолучение\tесть\t{pair}\t{pair}",
            ]
        if "МенеджерОбменаВерсииФормата" in code:
            if not box.has_correspondent:
                rows.append("МЕНЕДЖЕРВЕРСИИ\tнет узла")
            elif box.manager_other:
                rows.append("МЕНЕДЖЕРВЕРСИИ\tдругой")
            else:
                rows.append("МЕНЕДЖЕРВЕРСИИ\tтот")
        if box.pko_dup:
            rows.append(f"ДУБЛЬ\t{box.pko_dup}")
        return _ok(rows)

    def _take_value(self, code: str) -> str:
        found = re.search(r"\] = (.*?); ОбъектДанных\.Записать", code)
        assert found is not None
        value = eval_bsl(found.group(1))
        box = self.box
        box.value = value
        box.values.append(value)
        if box.auto_register:
            box.registered = True
        return value

    def _write(self, code: str) -> str:
        value = self._take_value(code)
        return _ok([f"ЗНАЧЕНИЕ\t{ed.show(value)}"])

    def _register(self, code: str) -> str:
        box = self.box
        if "УдалитьРегистрациюИзменений" in code:
            box.foreign = 0
        box.registered = True
        return _ok(["РЕГИСТРАЦИЯ\tда"])

    def _unregister(self, code: str) -> str:
        assert "УдалитьРегистрациюИзменений(Узел, Ссылка)" in code
        self.box.registered = False
        return _ok(["ОСТАЛОСЬ	0"])

    def _changes(self, code: str) -> str:
        _ = code
        box = self.box
        found = 1 if box.registered else 0
        return _ok([f"НАЙДЕНО\t{found}", f"ЧУЖИЕ\t{box.foreign}"])

    def _export(self, code: str) -> str:
        _ = code
        box = self.box
        if not box.freeze_number:
            box.sent += 1
        number = box.sent
        include = None if box.value == "" and not box.force_property else box.value
        payload = message_xml(
            number=number,
            sender=box.this_code,
            receiver=box.correspondent_code,
            ref=box.ref,
            value=include,
        )
        path = f"mem-export-{box.export_n}"
        box.export_n += 1
        box.files[path] = payload
        return _ok([f"ФАЙЛ\t{path}", f"РАЗМЕР\t{len(payload)}", f"НОМЕР\t{number}"])

    def _read_chunk(self, code: str) -> str:
        path = _quoted(code, "ИмяФайлаЧтения")
        offset = int(_number_in(code, "Смещение"))
        length = int(_number_in(code, "ДлинаЧтения"))
        data = self.box.files[path]
        piece = data[offset : offset + length]
        raw = base64.b64encode(piece).decode("ascii")
        if not ("Символы.ВК" in code and "Символы.ПС" in code and "СтрЗаменить" in code):
            raw = "\n".join(raw[index : index + 76] for index in range(0, len(raw), 76))
        return _ok([f"КУСОК\t{raw}", f"ПРОЧИТАНО\t{len(piece)}"])

    def _delete_file(self, code: str) -> str:
        path = _quoted(code, "УдалитьФайлы")
        self.box.files.pop(path, None)
        return _ok(["УДАЛЕН\tда"])

    def _begin_file(self, code: str) -> str:
        _ = code
        box = self.box
        path = f"mem-in-{box.import_n}"
        box.import_n += 1
        box.files[path] = b""
        return _ok([f"ФАЙЛ\t{path}"])

    def _append(self, code: str) -> str:
        path = _quoted(code, "ФайловыйПоток")
        raw = re.search(r'СтрЗаменить\(СтрЗаменить\("([A-Za-z0-9+/=\s]+)"', code)
        if raw is None:
            raw = re.search(r'Base64Значение\("([A-Za-z0-9+/=]+)"\)', code)
        assert raw is not None
        payload = re.sub(r"\s+", "", raw.group(1))
        self.box.files[path] = self.box.files.get(path, b"") + base64.b64decode(payload)
        return _ok(["ДОПИСАН\tда"])

    def _import(self, code: str) -> str:
        if self.box.reject_import:
            return "ОШИБКА Сообщение обмена было принято ранее"
        self._consume(_file_of(code, "ИмяФайла"))
        if self.box.warn_import:
            name, synonym = "ВыполненоСПредупреждениями", "Выполнено с предупреждениями"
        else:
            name, synonym = "Выполнено", "Выполнено"
        result = name if _enum_name(code) else synonym
        return _ok(["РЕЗУЛЬТАТ\t" + result, f"НОМЕР\t{self.box.received}"])

    def _consume(self, path: str) -> None:
        root = etree.fromstring(self.box.files[path])
        number = 0
        ref = ""
        comment: str | None = None
        for element in root.iter():
            name = etree.QName(element).localname
            if name == "MessageNo":
                number = int((element.text or "").strip())
            elif name == "Ссылка" and not ref:
                ref = (element.text or "").strip()
            elif name == "Комментарий":
                comment = "".join(str(part) for part in element.itertext())
        box = self.box
        box.received = number
        box.ref = ref or box.ref
        box.has_object = True
        if not box.object_name:
            box.object_name = "имя из сообщения"
        if comment is not None:
            box.value = comment
        elif box.absent_policy == "clear":
            box.value = ""

    def _query(self, code: str) -> str:
        _ = code
        box = self.box
        if box.hide_object or not box.has_object:
            return _ok(["СТРОК\t0"])
        value = box.value if box.lie_value is None else box.lie_value
        return _ok(["СТРОК\t1", f"УИД\t{box.ref}", f"ЗНАЧЕНИЕ\t{ed.show(value)}"])

    def _delete_object(self, code: str) -> str:
        asked = re.search(r'УникальныйИдентификатор\("([^"]+)"\)', code)
        uid = asked.group(1) if asked else ""
        box = self.box
        if not (box.has_object and uid == box.ref):
            return _ok(["УДАЛЕН\tнет"])
        expected = re.search(r"Наименование <> (.*?) Тогда", code)
        if expected is not None and eval_bsl(expected.group(1)) != box.object_name:
            return _ok(["УДАЛЕН\tчужое"])
        box.has_object = False
        return _ok(["УДАЛЕН\tда"])

    def _delete_node(self, code: str) -> str:
        found = re.search(r'НайтиПоКоду\("([^"]+)"\)', code)
        code_value = found.group(1) if found else ""
        box = self.box
        if not (box.has_correspondent and code_value == box.correspondent_code):
            return _ok(["УДАЛЕН\tнет"])
        expected = re.search(r"Наименование <> (.*?) Тогда", code)
        if expected is not None and eval_bsl(expected.group(1)) != box.node_name:
            return _ok(["УДАЛЕН\tчужое"])
        box.has_correspondent = False
        return _ok(["УДАЛЕН\tда"])

    def _file_size(self, code: str) -> str:
        path = _quoted(code, "ДвоичныеДанные")
        size = len(self.box.files.get(path, b"")) + self.box.size_bias
        return _ok([f"РАЗМЕР\t{size}"])

    def _registrations(self, code: str) -> str:
        _ = code
        box = self.box
        if not box.registered:
            return _ok([])
        return _ok([f"РЕГ\t{PLAN}\t{box.correspondent_code}"])


def _allowed_tables(box: _Box) -> set[str]:
    base = {META, f"{META}.Изменения", "РегистрСведений.СостоянияОбменовДанными"}
    if box.tables is not None:
        return box.tables
    return base


def _forbidden_table(code: str, allowed: set[str]) -> str:
    """Таблица запроса вне объявленного набора — как ответ платформы «Таблица не найдена»."""
    if "ВыбратьИзменения" in code:
        return "ВыбратьИзменения"
    for match in re.finditer(r"ИЗ\s+([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_.]*)", code):
        table = match.group(1)
        if table not in allowed:
            return table
    return ""


def _table_miss(table: str) -> str:
    return (
        "ОШИБКА Ошибка при вызове метода контекста (Выполнить)\n"
        "{ВнешняяОбработка.Проверка.Модуль(1)}:Запрос.Выполнить()\n"
        "[ОшибкаВоВремяВыполненияВстроенногоЯзыка]\n"
        "по причине:\n"
        f'{{(1, 40)}}: Таблица не найдена "{table}"'
    )


def _enum_name(code: str) -> bool:
    return "ИмяЗначенияПеречисления" in code or "XMLСтрока(" in code


def _ok(lines: list[str]) -> str:
    return "OK\n" + "\n".join([*lines, ed.ANSWER_END])


def _stage(code: str) -> str:
    found = re.search(r'СтадияПроверки = "([^"]*)"', code)
    return found.group(1) if found else ""


def _quoted(code: str, marker: str) -> str:
    found = re.search(rf'{re.escape(marker)}(?: = |\()"([^"]*)"', code)
    assert found is not None, marker
    return found.group(1)


def _number_in(code: str, name: str) -> str:
    found = re.search(rf"{name} = (\d+);", code)
    assert found is not None, name
    return found.group(1)


def _file_of(code: str, marker: str) -> str:
    return _quoted(code, marker)


def eval_bsl(expr: str) -> str:
    """Собирает выражение из литералов и `Символы.ПС`, как его строит `bsl_expr`."""
    expr = expr.strip().removesuffix(";").strip()
    out: list[str] = []
    index = 0
    while index < len(expr):
        if expr[index].isspace() or expr[index] == "+":
            index += 1
            continue
        if expr.startswith("Символы.ПС", index):
            out.append("\n")
            index += len("Символы.ПС")
            continue
        if expr[index] != '"':
            raise AssertionError(expr[index:])
        index += 1
        chars: list[str] = []
        while index < len(expr):
            if expr[index] == '"':
                if index + 1 < len(expr) and expr[index + 1] == '"':
                    chars.append('"')
                    index += 2
                    continue
                index += 1
                break
            chars.append(expr[index])
            index += 1
        out.append("".join(chars))
    return "".join(out)


def message_xml(
    *,
    number: int,
    sender: str,
    receiver: str,
    ref: str,
    value: str | None,
) -> bytes:
    comment = ""
    if value is not None:
        comment = (
            "<ОбщиеСвойстваОбъектовФормата>"
            f"<Комментарий>{escape(value)}</Комментарий>"
            "</ОбщиеСвойстваОбъектовФормата>"
        )
    text = (
        f'<msg:Message xmlns:msg="{MSG}">'
        "<msg:Header><msg:Format>"
        f"{FORMAT}</msg:Format><msg:Confirmation>"
        f"<msg:MessageNo>{number}</msg:MessageNo>"
        f"<msg:From>{sender}</msg:From><msg:To>{receiver}</msg:To>"
        f"<msg:ExchangePlan>{PLAN}</msg:ExchangePlan>"
        "</msg:Confirmation></msg:Header>"
        f'<Body xmlns="{FORMAT}">'
        f"<{META}><КлючевыеСвойства><Ссылка>{ref}</Ссылка></КлючевыеСвойства>"
        f"{comment}</{META}></Body></msg:Message>"
    )
    return text.encode("utf-8")


def _write_case(folder: Path, mutate: Callable[[dict[str, object]], None] | None = None) -> Path:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(data)
    path = folder / "case.json"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def _hubs(
    source: dict[str, object] | None = None, target: dict[str, object] | None = None
) -> dict[str, Hub]:
    source_box = _box(role="source", this_code=TGT, correspondent_code=SRC, **(source or {}))
    target_box = _box(role="target", this_code=SRC, correspondent_code=TGT, **(target or {}))
    return {
        "alpha.sandbox": Hub("alpha.sandbox", source_box),
        "beta.sandbox": Hub("beta.sandbox", target_box),
    }


def _install(monkeypatch: pytest.MonkeyPatch, hubs: dict[str, Hub], runs: Path) -> None:
    monkeypatch.setattr(ed, "RUNS", runs)
    monkeypatch.setattr(ed, "RAW_CHUNK", 64)

    def server_for(ref: str, root: Path | None = None) -> Hub:
        _ = root
        return hubs[ref]

    monkeypatch.setattr(ed, "server_for", server_for)


def _main(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str]:
    with pytest.raises(SystemExit) as caught:
        ed.main(argv)
    code = caught.value.code
    assert isinstance(code, int)
    return code, capsys.readouterr().out


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hubs: dict[str, Hub],
    argv: list[str],
) -> tuple[int, str, Path]:
    runs = tmp_path / "run"
    runs.mkdir()
    _install(monkeypatch, hubs, runs)
    code, out = _main(capsys, argv)
    return code, out, runs


def _assert_secret_free(text: str) -> None:
    assert SECRET not in text
    assert TOKEN not in text
    assert "Authorization" not in text


def _codes(hub: Hub) -> str:
    return "\n".join(hub.box.codes)


def _stages(hub: Hub) -> list[str]:
    return [_stage(code) for code in hub.box.codes]


def _use(data: dict[str, object]) -> None:
    nodes = data["nodes"]
    assert isinstance(nodes, dict)
    nodes["mode"] = "use"
    del nodes["variant"]
    obj = data["object"]
    assert isinstance(obj, dict)
    obj["create"] = False
    obj["ref"] = REF
    del obj["name"]


def _snippets() -> dict[str, str]:
    return {
        "state": ed.state_code(PLAN, "МенеджерОбменаПример", META, "Пример_Заметка", REF, "Имя"),
        "create_node": ed.create_node_code(PLAN, SRC, "1.20", "ОбменПример", True),
        "create_object": ed.create_object_code(META, 'Имя "а"\nб'),
        "hook": ed.hook_code(
            "МенеджерОбменаПример",
            "2",
            "Справочник_ПримерОбъекты_Отправка",
            "Пример_Заметка",
            "Комментарий",
            PLAN,
            SRC,
            "1.20",
            "Отправка",
        ),
        "write": ed.write_code(META, REF, "Пример_Заметка", 'кавычка "раз"\nвторая'),
        "register": ed.register_code(PLAN, SRC, META, REF, True),
        "register_keep": ed.register_code(PLAN, SRC, META, REF, False),
        "changes": ed.changes_code(PLAN, SRC, META, REF),
        "export": ed.export_code(PLAN, SRC),
        "read_chunk": ed.read_chunk_code("mem-export-0", 0, 64),
        "delete_file": ed.delete_file_code("mem-export-0"),
        "begin_file": ed.begin_file_code(),
        "append": ed.append_chunk_code("mem-in-0", "QQ=="),
        "import": ed.import_code(PLAN, TGT, "mem-in-0"),
        "query": ed.query_code(META, REF, "Пример_Заметка"),
        "delete_object": ed.delete_object_code(META, REF, "Пример обмена: должность проверки"),
        "delete_node": ed.delete_node_code(PLAN, SRC),
        "file_size": ed.file_size_code("mem-in-0"),
        "registrations": ed.registrations_code(META, REF),
    }


@pytest.mark.parametrize("name", list(_snippets()))
def test_snippets_are_one_guarded_line(name: str) -> None:
    code = _snippets()[name]
    assert "\n" not in code
    assert code.startswith("Попытка ") and code.endswith("КонецПопытки;")
    assert not NEW_CALL.search(code)
    assert "ПОДОБНО" not in code


def test_bsl_expr_quotes_and_newlines() -> None:
    assert ed.bsl_expr('кавычка "раз"\nвторая') == '"кавычка ""раз""" + Символы.ПС + "вторая"'
    code = ed.write_code(META, REF, "Пример_Заметка", 'x"; ВызватьИсключение "y')
    assert "\n" not in code
    assert '"x""; ВызватьИсключение ""y"' in code
    assert code.count("ВызватьИсключение") == 2
    named = ed.create_object_code(META, 'Имя "а"\nб')
    assert "\n" not in named
    assert "Символы.ПС" in named
    assert '""а""' in named
    assert "ПОДОБНО" not in named


def test_example_case_loads_and_is_anonymous() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    case = ed.load_case(EXAMPLE)
    assert case.plan == PLAN
    assert case.nodes.mode == "create"
    assert case.absent == "unknown"
    for marker in ("192.168", "Зарплата", "Бухгалтерия", SECRET):
        assert marker not in text


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (
            lambda data: data.__setitem__("code", "ВызватьИсключение"),
            "неизвестное поле случая: code",
        ),
        (lambda data: data.pop("plan"), "нет поля случая: plan"),
        (lambda data: data.__setitem__("absent", "drop"), "absent: нужно keep, clear или unknown"),
        (lambda data: data.__setitem__("manager_interface", "3"), "компоненты обмена"),
        (
            lambda data: data.__setitem__(
                "object",
                {"metadata": "Документ.ПримерДокумент", "create": True, "name": "Документ"},
            ),
            "справочника",
        ),
        (lambda data: data["values"].pop("empty"), "values.empty"),
    ],
)
def test_case_rejects_unknown_and_missing_fields(
    tmp_path: Path, mutate: Callable[[dict[str, object]], None], fragment: str
) -> None:
    path = _write_case(tmp_path, mutate)
    with pytest.raises(ed.CaseError, match=fragment):
        ed.load_case(path)


def test_case_error_is_protocol_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_case(tmp_path, lambda data: data.__setitem__("code", "Выполнить"))
    hubs = _hubs()
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        ["run", "--source", "alpha.sandbox", "--target", "beta.sandbox", "--case", str(path)],
    )
    assert code == 1
    assert "ОШИБКА неизвестное поле случая: code" in out
    assert "Traceback" not in out
    assert hubs["alpha.sandbox"].box.codes == []


@pytest.mark.parametrize("command", ["inspect", "run"])
def test_refuses_base_without_sandbox_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "projects.yaml").write_text(
        """
projects:
  alpha:
    name: Альфа
    mcp_config: .mcp.json
    configurations: {full: {dump: main}}
    bases:
      sandbox:
        {role: песочница, configuration: full, connection: 'File="C:/sb";', data_mcp: data-a}
      prod: {role: боевая, configuration: full, connection: 'File="C:/pr";', data_mcp: data-a}
""",
        encoding="utf-8",
    )
    (root / "projects.local.yaml").write_text("projects: {}\n", encoding="utf-8")
    monkeypatch.setattr(ed, "ROOT", root)
    called: list[str] = []

    def call(self: ed.DataServer, code: str) -> str:
        called.append(code)
        return ""

    monkeypatch.setattr(ed.DataServer, "call", call)
    case = _write_case(tmp_path)
    code, out = _main(
        capsys,
        [command, "--source", "alpha.prod", "--target", "alpha.sandbox", "--case", str(case)],
    )
    assert code == 1
    assert "песочницами" in out
    assert called == []
    assert "Traceback" not in out


def test_inspect_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs()
    case = _write_case(tmp_path)
    code, out, runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        ["inspect", "--source", "alpha.sandbox", "--target", "beta.sandbox", "--case", str(case)],
    )
    assert code == 0
    assert "ИТОГ OK" in out and out.rstrip().endswith("КОНЕЦ")
    assert "ПримерРасширение" in out and "безоп=Нет" in out
    blob = _codes(hubs["alpha.sandbox"]) + _codes(hubs["beta.sandbox"])
    assert ".Записать()" not in blob
    assert "СоздатьУзел" not in blob
    assert "Удалить" not in blob
    assert list(runs.glob("ed-exchange-*")) == []
    _assert_secret_free(out)


def test_successful_run_sees_value_in_message_and_receiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs()
    case = _write_case(tmp_path)
    code, out, runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        ["run", "--source", "alpha.sandbox", "--target", "beta.sandbox", "--case", str(case)],
    )
    assert code == 0, out
    order = [
        "ПЛАН ОбменПример",
        "ВЕРСИЯ 1.20",
        "ИСТОЧНИК alpha.sandbox → ПРИЕМНИК beta.sandbox",
        "СОСТОЯНИЕ ДО OK",
        "ИСТОЧНИК ОБЪЕКТ не запрашивался",
        "ИСТОЧНИК ИМЕНА 0",
        "ПЕРЕХВАТЧИК ИСТОЧНИК OK",
        "Отправка ПКС=1 Получение ПКС=0",
        "ПЕРЕХВАТЧИК ПРИЕМНИК OK",
        "Получение ПКС=1 Отправка ПКС=0",
        "УЗЛЫ OK",
        f"источник создан {SRC}",
        f"приёмник создан {TGT}",
        "МЕНЕДЖЕР ВЕРСИИ OK",
        "ОБЪЕКТ OK",
        f"создан {REF}",
        "ЗАПИСЬ v1 OK",
        "РЕГИСТРАЦИЯ v1 OK",
        "очищены=да",
        "ВЫГРУЗКА v1 OK",
        "номер=1",
        "СООБЩЕНИЕ v1 OK",
        "Значение шага 1",
        "по схеме не проверено",
        "ЗАГРУЗКА v1 OK",
        "ПРИЕМНИК v1 OK",
        "строк=1",
        "ЗАПИСЬ v2 OK",
        "СООБЩЕНИЕ v2 OK",
        "номер=2",
        "ПРИЕМНИК v2 OK",
        "Значение шага 2",
        "ЗАПИСЬ пусто OK",
        "СООБЩЕНИЕ пусто ФАКТ свойства нет",
        "ПРИЕМНИК пусто ФАКТ",
        "значение=Значение шага 2",
        "СОСТОЯНИЕ ПОСЛЕ OK",
        f"ИЗМЕНЕНО alpha.sandbox узел {SRC} номер отправленного: не было → 3",
        f"ИЗМЕНЕНО alpha.sandbox узел {SRC} номер принятого: не было → 0",
        f"ИЗМЕНЕНО beta.sandbox узел {TGT} номер отправленного: не было → 0",
        f"ИЗМЕНЕНО beta.sandbox узел {TGT} номер принятого: не было → 3",
        f"ИСТОЧНИК РЕГИСТРАЦИИ {PLAN} {SRC}",
        "ПРИЕМНИК РЕГИСТРАЦИИ нет",
        f"СОЗДАНО alpha.sandbox узел {SRC}",
        f"СОЗДАНО beta.sandbox узел {TGT}",
        f"СОЗДАНО alpha.sandbox {META} {REF}",
        f"СОЗДАНО beta.sandbox {META} {REF}",
        "ИТОГ OK",
        "КОНЕЦ",
    ]
    cursor = 0
    for prefix in order:
        pos = out.find(prefix, cursor)
        assert pos >= 0, prefix + "\n" + out
        cursor = pos
    folder = next(runs.glob("ed-exchange-*"))
    protocol = (folder / "protocol.txt").read_text(encoding="utf-8")
    assert protocol == out if out.endswith("\n") else out + "\n"
    assert (folder / "message-v1.xml").read_bytes().startswith(b"<msg:Message")
    assert "Значение шага 1".encode() in (folder / "message-v1.xml").read_bytes()
    assert "Комментарий".encode() not in (folder / "message-пусто.xml").read_bytes()
    ledger = json.loads((folder / "created.json").read_text(encoding="utf-8"))
    assert ledger["producer"] == "ed_exchange_check"
    assert STRANGER not in json.dumps(ledger)
    register = next(item for item in hubs["alpha.sandbox"].box.codes if _stage(item) == "register")
    assert register.index("УдалитьРегистрациюИзменений") < register.index(
        "ЗарегистрироватьИзменения"
    )
    assert _stages(hubs["alpha.sandbox"]).count("read-chunk") > 1
    assert _stages(hubs["beta.sandbox"]).count("append-chunk") > 1
    assert _stages(hubs["beta.sandbox"]).count("file-size") == 3
    chunk = next(item for item in hubs["alpha.sandbox"].box.codes if _stage(item) == "read-chunk")
    assert "Символы.ВК" in chunk and "Символы.ПС" in chunk
    assert "СтандартныеРеквизиты" in _codes(hubs["alpha.sandbox"])
    blob = out + protocol + (folder / "created.json").read_text(encoding="utf-8")
    _assert_secret_free(blob)
    assert "Traceback" not in out


@pytest.mark.parametrize(
    ("where", "flag", "failed", "skipped", "absent"),
    [
        ("source", "state", "СОСТОЯНИЕ ДО", "УЗЛЫ", "create-node"),
        ("source", "hook", "ПЕРЕХВАТЧИК ИСТОЧНИК", "ЗАПИСЬ v1", "write"),
        ("source", "write", "ЗАПИСЬ v1", "РЕГИСТРАЦИЯ v1", "register"),
        ("source", "export", "ВЫГРУЗКА v1", "СООБЩЕНИЕ v1", "read-chunk"),
        ("target", "import", "ЗАГРУЗКА v1", "ПРИЕМНИК v1", "query"),
        ("target", "query", "ПРИЕМНИК v1", "ЗАПИСЬ v2", "write"),
    ],
)
def test_failed_stage_does_not_become_next_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
    flag: str,
    failed: str,
    skipped: str,
    absent: str,
) -> None:
    source_flags: dict[str, object] = {}
    target_flags: dict[str, object] = {}
    (source_flags if where == "source" else target_flags)["fail_stage"] = flag
    hubs = _hubs(source_flags, target_flags)
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert f"{failed} ОШИБКА" in out
    assert f"{skipped} ПРОПУЩЕНА предыдущая стадия не пройдена" in out
    assert f"{skipped} OK" not in out
    watched = hubs["alpha.sandbox" if where == "source" else "beta.sandbox"]
    assert absent not in _stages(watched)
    assert "ИТОГ ОШИБКА" in out


def test_hook_without_pair_names_safe_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs({"hook_silent": True, "safe": True})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ПЕРЕХВАТЧИК ИСТОЧНИК ОШИБКА" in out
    assert "безопасн" in out
    assert "ЗАПИСЬ v1 ПРОПУЩЕНА" in out
    assert "write" not in _stages(hubs["alpha.sandbox"])
    assert "create-node" not in _stages(hubs["alpha.sandbox"])
    assert "create-object" not in _stages(hubs["alpha.sandbox"])


def test_registration_on_write_failure_skips_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def flag(data: dict[str, object]) -> None:
        data["check_registration_on_write"] = True

    hubs = _hubs()
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, flag)),
        ],
    )
    assert code == 1
    assert "РЕГИСТРАЦИЯ v1 ОШИБКА" in out
    assert "не зарегистрирован" in out
    assert "ВЫГРУЗКА v1 ПРОПУЩЕНА" in out
    blob = _codes(hubs["alpha.sandbox"])
    assert "ЗарегистрироватьИзменения" not in blob
    assert "export" not in _stages(hubs["alpha.sandbox"])


def test_registration_on_write_success_has_no_explicit_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def flag(data: dict[str, object]) -> None:
        data["check_registration_on_write"] = True

    hubs = _hubs({"auto_register": True})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, flag)),
        ],
    )
    assert code == 0, out
    assert "РЕГИСТРАЦИЯ v1 OK при записи" in out
    assert "ЗарегистрироватьИзменения" not in _codes(hubs["alpha.sandbox"])


def test_stale_registration_is_not_registration_on_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Регистрация, оставшаяся от прежнего обмена, снимается до записи и успехом не считается."""

    def flag(data: dict[str, object]) -> None:
        data["check_registration_on_write"] = True

    hubs = _hubs()
    hubs["alpha.sandbox"].box.registered = True
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, flag)),
        ],
    )
    assert code == 1
    assert "РЕГИСТРАЦИЯ v1 ОШИБКА" in out and "не зарегистрирован" in out
    stages = _stages(hubs["alpha.sandbox"])
    assert stages.index("unregister") < stages.index("write")


def test_use_mode_does_not_clear_foreign_registrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(
        {"has_correspondent": True, "has_object": True, "ref": REF, "foreign": 2},
        {"has_correspondent": True, "has_object": True, "ref": REF},
    )
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, _use)),
        ],
    )
    assert code == 1
    assert "РЕГИСТРАЦИЯ v1 ОШИБКА" in out
    assert "чужие" in out
    assert "регистрация не выполнялась" in out
    assert "ЗарегистрироватьИзменения" not in _codes(hubs["alpha.sandbox"])
    assert "export" not in _stages(hubs["alpha.sandbox"])
    changes = next(item for item in hubs["alpha.sandbox"].box.codes if _stage(item) == "changes")
    assert f"ИЗ {META}.Изменения" in changes
    assert f"ПланОбмена.{PLAN}.Изменения" not in changes
    assert "ВыбратьИзменения" not in changes
    assert "ПОДОБНО" not in _codes(hubs["alpha.sandbox"])


def test_values_roundtrip_quotes_and_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def awkward(data: dict[str, object]) -> None:
        obj = data["object"]
        assert isinstance(obj, dict)
        obj["name"] = 'Имя "проверка"\nвторая'
        values = data["values"]
        assert isinstance(values, dict)
        values["v1"] = 'кавычка "раз"'
        values["v2"] = "строка\nдва"
        data["absent_step"] = False

    hubs = _hubs()
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, awkward)),
        ],
    )
    assert code == 0, out
    assert hubs["alpha.sandbox"].box.created_name == 'Имя "проверка"\nвторая'
    assert hubs["alpha.sandbox"].box.values == ['кавычка "раз"', "строка\nдва"]
    assert "ПРИЕМНИК v1 OK строк=1 уид=" in out
    assert 'значение=кавычка "раз"' in out
    assert "значение=строка⏎два" in out
    assert "ОТСУТСТВИЕ ПРОПУЩЕНА шаг не запрошен случаем" in out
    assert "ЗАПИСЬ пусто" not in out
    assert "ПОДОБНО" not in _codes(hubs["alpha.sandbox"])


def test_absent_clear_expects_empty_receiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def clear(data: dict[str, object]) -> None:
        data["absent"] = "clear"

    hubs = _hubs(target={"absent_policy": "clear"})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, clear)),
        ],
    )
    assert code == 0, out
    assert "СООБЩЕНИЕ пусто OK свойства нет" in out
    assert "ПРИЕМНИК пусто OK" in out
    assert "значение=" in out.split("ПРИЕМНИК пусто OK", 1)[1].splitlines()[0]


def test_absent_keep_fails_when_receiver_lost_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def keep(data: dict[str, object]) -> None:
        data["absent"] = "keep"

    hubs = _hubs(target={"absent_policy": "clear"})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, keep)),
        ],
    )
    assert code == 1
    assert "ПРИЕМНИК v2 OK" in out
    assert "ПРИЕМНИК пусто ОШИБКА" in out
    assert "ожидалось Значение шага 2" in out
    assert "СОСТОЯНИЕ ПОСЛЕ OK" in out


def test_present_property_fails_absent_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def keep(data: dict[str, object]) -> None:
        data["absent"] = "keep"

    hubs = _hubs({"force_property": True})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, keep)),
        ],
    )
    assert code == 1
    assert "СООБЩЕНИЕ пусто ОШИБКА" in out
    assert "свойство есть" in out
    assert "ЗАГРУЗКА пусто ПРОПУЩЕНА" in out


def test_duplicate_message_is_not_a_successful_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(target={"reject_import": True})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ЗАГРУЗКА v1 ОШИБКА" in out
    assert "принято ранее" in out
    assert "query" not in _stages(hubs["beta.sandbox"])


def test_write_reread_does_not_write_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs({"drop_stage": "write", "drop_left": 1, "apply_on_drop": True})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 0, out
    assert "ЗАПИСЬ v1 OK" in out
    assert "повторным чтением" in out
    assert _stages(hubs["alpha.sandbox"]).count("write") == 3


def test_write_not_applied_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs({"drop_stage": "write", "drop_left": 1, "apply_on_drop": False})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ЗАПИСЬ v1 ОШИБКА" in out
    assert "повтор записи не выполнялся" in out
    assert _stages(hubs["alpha.sandbox"]).count("write") == 1


def test_import_reread_does_not_import_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(target={"drop_stage": "import", "drop_left": 1, "apply_on_drop": True})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 0, out
    assert "ЗАГРУЗКА v1 OK" in out
    assert "повторным чтением" in out
    assert _stages(hubs["beta.sandbox"]).count("import") == 3


def test_old_message_number_is_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs({"freeze_number": True})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ВЫГРУЗКА v1 ОШИБКА" in out
    assert "сообщение не новое" in out
    assert "import" not in _stages(hubs["beta.sandbox"])
    assert _stages(hubs["alpha.sandbox"]).count("export") == 1


def test_occupied_name_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs({"name_count": 1})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ОБЪЕКТ ОШИБКА" in out
    assert "чужой объект не используется" in out
    assert "create-object" not in _stages(hubs["alpha.sandbox"])


def test_sync_constant_is_recorded_and_not_reverted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs({"sync": "Нет"})
    code, out, runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 0, out
    assert "ИЗМЕНЕНО alpha.sandbox константа ИспользоватьСинхронизациюДанных: Нет → Да" in out
    folder = next(runs.glob("ed-exchange-*"))
    cleanup, text = _main(capsys, ["cleanup", "--run-dir", str(folder)])
    assert cleanup == 0, text
    assert "ОСТАВЛЕНО alpha.sandbox константа ИспользоватьСинхронизациюДанных: Нет → Да" in text
    blob = _codes(hubs["alpha.sandbox"]) + _codes(hubs["beta.sandbox"])
    assert "Значение = Истина" not in blob.split("delete-object", 1)[-1]


def _dump(folder: Path, *, direct: bool = False) -> Path:
    packages = folder / "XDTOPackages"
    ext = packages / "ФорматПример" / "Ext"
    ext.mkdir(parents=True)
    (packages / "ФорматПример.xml").write_text(
        """<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.3/common">
  <XDTOPackage uuid="00000000-0000-0000-0000-000000000001"><Properties>
    <Name>ФорматПример</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>1.20</v8:content></v8:item></Synonym>
    <Comment/><Namespace>http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.20</Namespace>
  </Properties></XDTOPackage>
</MetaDataObject>
""",
        encoding="utf-8",
    )
    if direct:
        body = (
            '<package xmlns="http://v8.1c.ru/8.1/xdto" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            f'targetNamespace="{FORMAT}">'
            f'<objectType name="{META}">'
            '<property name="Комментарий" type="xs:string"/></objectType>'
            "</package>"
        )
    else:
        body = (
            '<package xmlns="http://v8.1c.ru/8.1/xdto" '
            f'xmlns:t="{FORMAT}" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            f'targetNamespace="{FORMAT}">'
            '<objectType name="ОбщиеСвойстваОбъектовФормата">'
            '<property name="Комментарий" type="xs:string"/></objectType>'
            f'<objectType name="{META}">'
            '<property name="ОбщиеСвойстваОбъектовФормата" type="t:ОбщиеСвойстваОбъектовФормата"/>'
            "</objectType></package>"
        )
    (ext / "Package.bin").write_text(body, encoding="utf-8")
    return folder


def test_schema_path_resolves_wrapped_property(tmp_path: Path) -> None:
    dump = _dump(tmp_path / "dump")
    assert (
        ed.schema_property_path(dump, "1.20", META, "Комментарий")
        == "ОбщиеСвойстваОбъектовФормата/Комментарий"
    )


def test_message_check_uses_schema_when_dump_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dump = _dump(tmp_path / "dump")
    hubs = _hubs()
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
            "--source-dump",
            str(dump),
        ],
    )
    assert code == 0, out
    assert "СООБЩЕНИЕ v1 OK" in out
    assert "по схеме проверено" in out
    assert "по схеме не проверено" not in out


def test_schema_path_mismatch_fails_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dump = _dump(tmp_path / "dump")

    def shorten(data: dict[str, object]) -> None:
        data["property_path"] = "Комментарий"

    hubs = _hubs()
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, shorten)),
            "--source-dump",
            str(dump),
        ],
    )
    assert code == 1
    assert "СООБЩЕНИЕ v1 ОШИБКА" in out
    assert "путь схемы" in out
    assert "import" not in _stages(hubs["beta.sandbox"])


def test_dumps_with_different_paths_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    left = _dump(tmp_path / "left")
    right = _dump(tmp_path / "right", direct=True)
    hubs = _hubs()
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
            "--source-dump",
            str(left),
            "--target-dump",
            str(right),
        ],
    )
    assert code == 1
    assert "СООБЩЕНИЕ v1 ОШИБКА" in out
    assert "различаются" in out


def test_check_message_requires_version_number_and_value() -> None:
    payload = message_xml(number=1, sender=TGT, receiver=SRC, ref=REF, value="Значение шага 1")
    present, text = ed.check_message(
        payload,
        version="1.20",
        number=1,
        sender=TGT,
        receiver=SRC,
        metadata=META,
        ref=REF,
        plan=PLAN,
        path="ОбщиеСвойстваОбъектовФормата/Комментарий",
        value="Значение шага 1",
    )
    assert present and text == "Значение шага 1"
    missing = message_xml(number=1, sender=TGT, receiver=SRC, ref=REF, value=None)
    present, text = ed.check_message(
        missing,
        version="1.20",
        number=1,
        sender=TGT,
        receiver=SRC,
        metadata=META,
        ref=REF,
        plan=PLAN,
        path="ОбщиеСвойстваОбъектовФормата/Комментарий",
        value=None,
    )
    assert not present and text == ""
    with pytest.raises(ed.StageFailed, match="нет свойства"):
        ed.check_message(
            missing,
            version="1.20",
            number=1,
            sender=TGT,
            receiver=SRC,
            metadata=META,
            ref=REF,
            plan=PLAN,
            path="ОбщиеСвойстваОбъектовФормата/Комментарий",
            value="Значение шага 1",
        )


def _ledger(folder: Path, producer: str = "ed_exchange_check") -> None:
    folder.mkdir(parents=True)
    (folder / "protocol.txt").write_text(
        f"ПЛАН {PLAN}\nВЕРСИЯ 1.20\nИСТОЧНИК alpha.sandbox → ПРИЕМНИК beta.sandbox\n"
        "ИТОГ OK\nКОНЕЦ\n",
        encoding="utf-8",
        newline="\n",
    )
    payload = {
        "producer": producer,
        "created": [
            {
                "base": "alpha.sandbox",
                "kind": "object",
                "metadata": META,
                "ref": REF,
                "name": "Пример обмена: должность проверки",
            },
            {"base": "beta.sandbox", "kind": "node", "plan": PLAN, "code": TGT},
        ],
        "modified": [
            {
                "base": "alpha.sandbox",
                "kind": "constant",
                "text": (
                    "ИЗМЕНЕНО alpha.sandbox константа ИспользоватьСинхронизациюДанных: Нет → Да"
                ),
            }
        ],
    }
    (folder / "created.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def test_cleanup_deletes_only_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "run"
    folder = runs / "ed-exchange-1"
    _ledger(folder)
    hubs = _hubs(
        {"has_object": True, "ref": REF, "object_name": "Пример обмена: должность проверки"},
        {"has_correspondent": True, "has_object": True, "ref": STRANGER},
    )
    _install(monkeypatch, hubs, runs)
    code, out = _main(capsys, ["cleanup", "--run-dir", str(folder)])
    assert code == 0, out
    assert f"ОЧИСТКА alpha.sandbox {META} {REF} да" in out
    assert f"ОЧИСТКА beta.sandbox узел {TGT} да" in out
    assert "ОСТАВЛЕНО alpha.sandbox константа ИспользоватьСинхронизациюДанных: Нет → Да" in out
    blob = _codes(hubs["alpha.sandbox"]) + _codes(hubs["beta.sandbox"])
    assert REF in blob
    assert TGT in blob
    assert STRANGER not in blob
    assert "ПОДОБНО" not in blob
    assert "НайтиПоНаименованию" not in blob
    assert "ИспользоватьСинхронизациюДанных" not in blob
    assert hubs["beta.sandbox"].box.has_object
    _assert_secret_free(out)
    assert (folder / "cleanup.txt").read_text(encoding="utf-8").startswith("ОЧИСТКА")


def test_cleanup_continues_after_a_failed_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "run"
    folder = runs / "ed-exchange-2"
    _ledger(folder)
    hubs = _hubs(
        {
            "fail_delete": True,
            "has_object": True,
            "ref": REF,
            "object_name": "Пример обмена: должность проверки",
        },
        {"has_correspondent": True},
    )
    _install(monkeypatch, hubs, runs)
    code, out = _main(capsys, ["cleanup", "--run-dir", str(folder)])
    assert code == 1
    assert "ОЧИСТКА ОШИБКА" in out
    assert f"ОЧИСТКА beta.sandbox узел {TGT} да" in out
    assert "delete-node" in _stages(hubs["beta.sandbox"])


def test_cleanup_refuses_foreign_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "run"
    folder = runs / "ed-exchange-3"
    _ledger(folder, producer="other")
    monkeypatch.setattr(ed, "RUNS", runs)
    called: list[str] = []

    def server_for(ref: str, root: Path | None = None) -> Hub:
        called.append(ref)
        raise AssertionError(ref)

    monkeypatch.setattr(ed, "server_for", server_for)
    code, out = _main(capsys, ["cleanup", "--run-dir", str(folder)])
    assert code == 1
    assert "не от этого скрипта" in out
    assert called == []


def test_cleanup_reports_missing_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "run"
    folder = runs / "ed-exchange-4"
    _ledger(folder)
    hubs = _hubs(target={"has_correspondent": True})
    _install(monkeypatch, hubs, runs)
    code, out = _main(capsys, ["cleanup", "--run-dir", str(folder)])
    assert code == 1
    assert f"ОЧИСТКА alpha.sandbox {META} {REF} нет" in out
    assert f"ОЧИСТКА beta.sandbox узел {TGT} да" in out


def _message(objects: str, *, plan: str = PLAN, number: int = 5) -> bytes:
    return (
        f'<msg:Message xmlns:msg="{MSG}"><msg:Header><msg:Format>{FORMAT}</msg:Format>'
        "<msg:Confirmation>"
        f"<msg:MessageNo>{number}</msg:MessageNo><msg:From>{TGT}</msg:From><msg:To>{SRC}</msg:To>"
        f"<msg:ExchangePlan>{plan}</msg:ExchangePlan>"
        f'</msg:Confirmation></msg:Header><Body xmlns="{FORMAT}">{objects}</Body></msg:Message>'
    ).encode()


def test_message_key_is_direct_child_not_parent_ref() -> None:
    child = (
        f"<{META}><КлючевыеСвойства><Ссылка>{STRANGER}</Ссылка></КлючевыеСвойства>"
        f"<Родитель><Ссылка>{REF}</Ссылка></Родитель>"
        "<ОбщиеСвойстваОбъектовФормата><Комментарий>значение ЧУЖОГО</Комментарий>"
        f"</ОбщиеСвойстваОбъектовФормата></{META}>"
    )
    ours = (
        f"<{META}><КлючевыеСвойства><Ссылка>{REF}</Ссылка></КлючевыеСвойства>"
        "<ОбщиеСвойстваОбъектовФормата><Комментарий>наше</Комментарий>"
        f"</ОбщиеСвойстваОбъектовФормата></{META}>"
    )

    def check(payload: bytes, value: str | None) -> tuple[bool, str]:
        return ed.check_message(
            payload,
            version="1.20",
            number=5,
            sender=TGT,
            receiver=SRC,
            plan=PLAN,
            metadata=META,
            ref=REF,
            path="ОбщиеСвойстваОбъектовФормата/Комментарий",
            value=value,
        )

    with pytest.raises(ed.StageFailed, match="нет объекта"):
        check(_message(child), "значение ЧУЖОГО")
    present, text = check(_message(ours + child), "наше")
    assert present and text == "наше"
    with pytest.raises(ed.StageFailed, match="несколько объектов"):
        check(_message(ours + ours), "наше")


def test_exchange_plan_must_match_the_case() -> None:
    body = f"<{META}><КлючевыеСвойства><Ссылка>{REF}</Ссылка></КлючевыеСвойства></{META}>"
    with pytest.raises(ed.StageFailed, match="план сообщения"):
        ed.check_message(
            _message(body, plan="ДругойПлан"),
            version="1.20",
            number=5,
            sender=TGT,
            receiver=SRC,
            plan=PLAN,
            metadata=META,
            ref=REF,
            path="ОбщиеСвойстваОбъектовФормата/Комментарий",
            value=None,
        )


def test_import_warning_synonym_is_the_enum_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(target={"warn_import": True})
    code, out, runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 0, out
    assert "ИмяЗначенияПеречисления" in _codes(hubs["beta.sandbox"])
    folder = next(runs.glob("ed-exchange-*"))
    ledger = json.loads((folder / "created.json").read_text(encoding="utf-8"))
    assert any(
        item.get("base") == "beta.sandbox" and item.get("kind") == "object"
        for item in ledger["created"]
    )


def test_receiver_object_is_journaled_before_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(target={"hide_object": True})
    code, out, runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ЗАГРУЗКА v1 OK" in out
    assert "ПРИЕМНИК v1 ОШИБКА" in out
    folder = next(runs.glob("ed-exchange-*"))
    ledger = json.loads((folder / "created.json").read_text(encoding="utf-8"))
    assert any(
        item.get("base") == "beta.sandbox" and item.get("ref") == REF for item in ledger["created"]
    )


def test_stale_received_number_blocks_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(
        {"has_correspondent": True, "sent": 4, "has_object": True, "ref": REF, "value": ""},
        {
            "has_correspondent": True,
            "received": 5,
            "has_object": True,
            "ref": REF,
            "value": "уже другое",
        },
    )

    def use(data: dict[str, object]) -> None:
        _use(data)
        data["absent_step"] = False

    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, use)),
        ],
    )
    assert code == 1
    assert "ЗАГРУЗКА v1 ОШИБКА" in out
    assert "уже не меньше" in out
    assert hubs["beta.sandbox"].box.import_n == 0


def test_json_error_after_create_rereads_and_keeps_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken(Hub):
        def call(self, code: str) -> str:
            if _stage(code) == "create-node":
                self.box.codes.append(code)
                self._open_node(code)
                raise json.JSONDecodeError("Expecting value", "<html>", 0)
            return super().call(code)

    hubs = _hubs()
    source = hubs["alpha.sandbox"].box
    hubs["alpha.sandbox"] = Broken("alpha.sandbox", source)
    code, out, runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert "УЗЛЫ OK" in out
    assert "повторным чтением" in out
    assert "Traceback" not in out
    folder = next(runs.glob("ed-exchange-*"))
    assert (folder / "protocol.txt").is_file()
    ledger = json.loads((folder / "created.json").read_text(encoding="utf-8"))
    assert any(
        item.get("kind") == "node" and item.get("base") == "alpha.sandbox"
        for item in ledger["created"]
    )
    assert code == 0, out


def test_stage_keeps_protocol_when_value_error_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines: list[str] = []
    flow = ed.Flow(lines)

    def boom() -> str:
        raise ValueError("битый разбор")

    flow.step("РАЗБОР", boom)
    assert flow.failed
    assert lines == ["РАЗБОР ОШИБКА битый разбор"]
    hubs = _hubs({"cut_marker": "register"})
    code, out, runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "РЕГИСТРАЦИЯ v1 ОШИБКА" in out
    assert "ответ обрезан" in out
    assert "Traceback" not in out
    assert next(runs.glob("ed-exchange-*/protocol.txt")).is_file()


def test_truncated_value_is_not_an_empty_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def clear(data: dict[str, object]) -> None:
        data["absent"] = "clear"

    hubs = _hubs({"cut_marker": "write"})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, clear)),
        ],
    )
    assert code == 1
    assert "ЗАПИСЬ v1 ОШИБКА" in out
    assert "ответ обрезан" in out
    assert "ПРИЕМНИК пусто OK" not in out


def test_values_must_differ_and_reject_distorted_characters(tmp_path: Path) -> None:
    def same(data: dict[str, object]) -> None:
        data["values"] = {"v1": "одно", "v2": "одно", "empty": "одно"}

    with pytest.raises(ed.CaseError, match="различаться"):
        ed.load_case(_write_case(tmp_path, same))
    nested = tmp_path / "nested"
    nested.mkdir()

    def distorted(data: dict[str, object]) -> None:
        values = data["values"]
        assert isinstance(values, dict)
        values["v1"] = "a\u2028b"

    with pytest.raises(ed.CaseError, match="искажает"):
        ed.load_case(_write_case(nested, distorted))
    for char in ("\x0b", "\x0c", "\x1c", "\x85", "→", "⏎"):
        slot = tmp_path / f"char-{ord(char)}"
        slot.mkdir()

        def put(data: dict[str, object], char: str = char) -> None:
            values = data["values"]
            assert isinstance(values, dict)
            values["v2"] = f"x{char}y"

        with pytest.raises(ed.CaseError, match="искажает"):
            ed.load_case(_write_case(slot, put))


def test_receiver_already_has_v1_fails_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(
        {"has_correspondent": True, "has_object": True, "ref": REF, "value": ""},
        {"has_correspondent": True, "has_object": True, "ref": REF, "value": "Значение шага 1"},
    )
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, _use)),
        ],
    )
    assert code == 1
    assert "ЗАПИСЬ v1 ОШИБКА" in out
    assert "уже значение v1" in out
    assert "write" not in _stages(hubs["alpha.sandbox"])


def test_occupied_target_name_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(target={"name_count": 1})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ОБЪЕКТ ОШИБКА" in out
    assert "приёмнике уже есть" in out
    assert "create-object" not in _stages(hubs["alpha.sandbox"])


def test_opposite_direction_leak_and_duplicate_pko_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leaked = _hubs({"leak": True})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        leaked,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ПЕРЕХВАТЧИК ИСТОЧНИК ОШИБКА" in out
    assert "Получение" in out
    assert "create-node" not in _stages(leaked["alpha.sandbox"])
    hook = ed.hook_code(
        "МенеджерОбменаПример", "2", "ПКО", "Реквизит", "Свойство", PLAN, SRC, "1.20", "Отправка"
    )
    assert "Для Каждого ПравилоОбхода Из Правила" in hook
    assert "МенеджерОбменаВерсииФормата" in hook

    duplicated = _hubs({"pko_dup": 2})
    second = tmp_path / "second"
    second.mkdir()
    code, out, _runs = _run(
        second,
        monkeypatch,
        capsys,
        duplicated,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(second)),
        ],
    )
    assert code == 0, out
    assert "ПКО ДУБЛЬ" in out


def test_manager_for_version_mismatch_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(
        {"has_correspondent": True, "manager_other": True},
        {"has_correspondent": True, "manager_other": True},
    )
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, _use)),
        ],
    )
    assert code == 1
    assert "ПЕРЕХВАТЧИК ИСТОЧНИК ОШИБКА" in out
    assert "менеджер" in out
    assert "write" not in _stages(hubs["alpha.sandbox"])


def test_file_size_mismatch_fails_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs(target={"size_bias": 3})
    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path)),
        ],
    )
    assert code == 1
    assert "ЗАГРУЗКА v1 ОШИБКА" in out
    assert "размер файла" in out
    assert "import" not in _stages(hubs["beta.sandbox"])


def test_after_state_lists_remaining_registrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hubs = _hubs({"auto_register": True})

    def flag(data: dict[str, object]) -> None:
        data["check_registration_on_write"] = True
        data["absent_step"] = False

    code, out, _runs = _run(
        tmp_path,
        monkeypatch,
        capsys,
        hubs,
        [
            "run",
            "--source",
            "alpha.sandbox",
            "--target",
            "beta.sandbox",
            "--case",
            str(_write_case(tmp_path, flag)),
        ],
    )
    assert code == 0, out
    assert f"ИСТОЧНИК РЕГИСТРАЦИИ {PLAN} {SRC}" in out
    assert "ВыбратьИзменения" not in _codes(hubs["alpha.sandbox"])


def test_optional_type_names_default_to_metadata(tmp_path: Path) -> None:
    case = ed.load_case(EXAMPLE)
    assert case.obj.target_metadata == META
    assert case.obj.format_name == META

    def rename(data: dict[str, object]) -> None:
        obj = data["object"]
        assert isinstance(obj, dict)
        obj["target_metadata"] = "Документ.ПримерДокумент"
        obj["format_name"] = "ПримерФормата"

    loaded = ed.load_case(_write_case(tmp_path, rename))
    assert loaded.obj.metadata == META
    assert loaded.obj.target_metadata == "Документ.ПримерДокумент"
    assert loaded.obj.format_name == "ПримерФормата"


def test_cleanup_refuses_directory_outside_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = tmp_path / "not-a-run"
    _ledger(foreign)
    hubs = _hubs(target={"has_object": True, "ref": STRANGER, "value": "чужое"})
    _install(monkeypatch, hubs, tmp_path / "run")
    code, out = _main(capsys, ["cleanup", "--run-dir", str(foreign)])
    assert code == 1
    assert "kdbase/run" in out
    assert hubs["beta.sandbox"].box.has_object
    assert not any("Удалить()" in item for item in hubs["beta.sandbox"].box.codes)


def test_cleanup_skips_name_mismatch_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "run"
    folder = runs / "ed-exchange-name"
    _ledger(folder)
    hubs = _hubs(
        {"has_object": True, "ref": REF, "object_name": "другое имя"},
        {"has_correspondent": True, "node_name": "чужой узел"},
    )
    _install(monkeypatch, hubs, runs)
    code, out = _main(capsys, ["cleanup", "--run-dir", str(folder)])
    assert code == 1
    assert "ПРОПУСК" in out
    assert "наименование не совпало" in out
    assert hubs["alpha.sandbox"].box.has_object
    assert hubs["beta.sandbox"].box.has_correspondent
    assert f"ОЧИСТКА ПРОПУСК beta.sandbox узел {TGT}" in out


def test_cleanup_refuses_base_without_sandbox_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "projects.yaml").write_text(
        """
projects:
  alpha:
    name: Альфа
    mcp_config: .mcp.json
    configurations: {full: {dump: main}}
    bases:
      sandbox:
        {role: песочница, configuration: full, connection: 'File="C:/sb";', data_mcp: data-a}
      prod: {role: боевая, configuration: full, connection: 'File="C:/pr";', data_mcp: data-a}
""",
        encoding="utf-8",
    )
    (root / "projects.local.yaml").write_text("projects: {}\n", encoding="utf-8")
    runs = tmp_path / "run"
    folder = runs / "ed-exchange-prod"
    _ledger(folder)
    ledger_path = folder / "created.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    payload["created"] = [
        {
            "base": "alpha.prod",
            "kind": "object",
            "metadata": META,
            "ref": REF,
            "name": "Пример обмена: должность проверки",
        }
    ]
    ledger_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ed, "ROOT", root)
    monkeypatch.setattr(ed, "RUNS", runs)
    called: list[str] = []

    def call(self: ed.DataServer, code: str) -> str:
        called.append(code)
        return ""

    monkeypatch.setattr(ed.DataServer, "call", call)
    code, out = _main(capsys, ["cleanup", "--run-dir", str(folder)])
    assert code == 1
    assert "песочницами" in out
    assert called == []


def test_query_aliases_are_not_reserved_words() -> None:
    """«Есть» — слово языка запросов (ЕСТЬ NULL): такой псевдоним на живой базе не разбирается."""
    ref = "00000000-0000-0000-0000-000000000000"
    codes = [
        ed.changes_code("План", "КОД", "Справочник.Пример", ref),
        ed.registrations_code("Справочник.Пример", ref),
    ]
    for code in codes:
        assert " КАК Есть " not in code
