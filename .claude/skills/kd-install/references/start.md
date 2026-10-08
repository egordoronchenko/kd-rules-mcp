# Шаги 0–1: что уже есть, образ или клон

## 0. Что уже есть

Выполнить и записать результат (строки одинаковы для PowerShell 7, Windows PowerShell 5.1 и bash):

```powershell
git --version; docker --version; docker compose version; uv --version
docker info --format '{{.ServerVersion}}'
```

Шаблон `--format` — в одинарных кавычках: двойные ломаются, когда клиент оборачивает строку в
`powershell -Command "…"` (`docker` отвечает «unknown shorthand flag»). Оболочка клиента недоступна, отвечает
ошибкой разбора или нечитаемым выводом (cp866) — те же строки в другой оболочке (bash ↔ PowerShell).

- Docker работает, а Python/uv или Git нет — по умолчанию **готовый образ**, ничего из них ставить не нужно.
  Образ подходит и при наличии uv. Клон выбирается для разработки/проверок через базы 1С или по просьбе человека.
  Только для пути из клона: нет `git`/`uv` — предложить установку
  (uv: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`), выполнить после согласия.
- Нет Docker или `docker info` падает — **закончить ход**: человек ставит/запускает Docker Desktop (WSL 2), потом
  продолжаем.

Уже установлен? Если есть папка с `pyproject.toml` проекта `kd-rules-mcp` и работает
`uv run python scripts/check_server.py` — перейти к нужному шагу (новый проект — шаг 3, подключение к другому
проекту — шаг 6, обновление — шаг 9).

## 1. Образ или клон

**Готовый образ (по умолчанию без Python/uv).** Спросить папку установки и выбрать опубликованный
тег с образом в релизах https://github.com/egordoronchenko/kd-rules-mcp/releases. В пустую папку
скачать `docker-compose.yml`, `projects.example.yaml`, `projects.local.example.yaml` по адресу
`https://raw.githubusercontent.com/egordoronchenko/kd-rules-mcp/<тег-vX.Y.Z>/<файл>`.
Команды PowerShell и sh — раздел «Готовый образ без клона» в
https://github.com/egordoronchenko/kd-rules-mcp/blob/main/docs/INSTALL.md.
Проверка: три файла есть, Docker отвечает; клона и uv для этого пути нет.

**Из исходников.**

Спросить, куда ставить (по умолчанию — рядом с проектами 1С). Затем:

```powershell
git clone https://github.com/egordoronchenko/kd-rules-mcp.git <папка>
cd <папка>
uv sync
```

Проверка: `uv run python -c "import kd_rules_mcp; print('ok')"` → `ok`.
