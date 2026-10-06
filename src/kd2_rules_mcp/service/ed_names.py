"""Область общих модулей из доступного XML-источника структуры, без исполнения BSL."""

from collections.abc import Callable, Mapping
from pathlib import Path

from lxml import etree

from kd2_rules_mcp.ed.lexer import tokenize


def _exported_methods(text: str) -> set[str]:
    """Скобки в строках и комментарии не прерывают экспортную сигнатуру."""
    tokens = tuple(t for t in tokenize(text) if t.kind not in ("comment", "directive"))
    result = set()
    for i, token in enumerate(tokens[:-2]):
        if (
            token.kind != "identifier"
            or token.folded not in ("процедура", "функция", "procedure", "function")
            or tokens[i + 1].kind != "identifier"
            or tokens[i + 2].value != "("
        ):
            continue
        depth = 1
        for j in range(i + 3, len(tokens)):
            if tokens[j].kind != "symbol":
                continue
            if tokens[j].value == "(":
                depth += 1
            elif tokens[j].value == ")":
                depth -= 1
                if depth == 0:
                    if j + 1 < len(tokens) and tokens[j + 1].folded in ("экспорт", "export"):
                        result.add(tokens[i + 1].value)
                    break
    return result


def common_module_names(
    metadata: Mapping[str, str],
    read_path: Callable[[str], Path],
    *,
    extensions: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """MD83Exp не содержит общей области модулей; отсутствие инвентаря не считается пустым."""
    if metadata.get("source") != "xml" or not metadata.get("source_path"):
        return None
    roots = (read_path(metadata["source_path"]), *(read_path(p) for p in extensions))
    if any(not root.is_dir() for root in roots):
        return None
    modules, functions = set(), set()
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    for root in roots:
        folder = root / "CommonModules"
        if not folder.is_dir():
            continue
        modules.update(p.stem for p in folder.glob("*.xml"))
        modules.update(p.name for p in folder.iterdir() if p.is_dir())
        for path in folder.glob("*.xml"):
            doc = etree.parse(str(path), parser)
            if not any(
                isinstance(n.tag, str)
                and etree.QName(n).localname == "Global"
                and (n.text or "").strip().casefold() == "true"
                for n in doc.iter()
            ):
                continue
            source = folder / path.stem / "Ext/Module.bsl"
            if source.is_file():
                # Только имена экспортных процедур/функций глобального общего модуля.
                functions.update(_exported_methods(source.read_text("utf-8-sig")))
    return tuple(sorted(modules)), tuple(sorted(functions))
