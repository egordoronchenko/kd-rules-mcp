"""MCP-сервер правил обмена «Конвертации данных 2»."""


def main() -> None:
    """Точка входа сервера (`kd-rules-mcp`): streamable HTTP, настройки из `KD2_*`."""
    # SDK MCP грузится только при запуске сервера, не при импорте пакета.
    from kd_rules_mcp.server import main as run

    run()
