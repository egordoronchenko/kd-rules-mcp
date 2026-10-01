---
name: kd2-install
description: Установить и подключить MCP-сервер kd2-rules-mcp (правила обмена «Конвертации данных 2» для 1С) — от клонирования до рабочего project_list, с вопросами человеку и проверкой после каждого шага. Использовать, когда просят «установи/настрой/подключи kd2-rules-mcp», подключить его к проекту 1С или добавить в него новый проект.
---

# Установка kd2-rules-mcp агентом

Человеческая версия тех же шагов — `docs/INSTALL.md` репозитория
(https://github.com/egordoronchenko/kd2-rules-mcp). Этот файл можно получить и без клона:
https://raw.githubusercontent.com/egordoronchenko/kd2-rules-mcp/main/.claude/skills/kd2-install/SKILL.md

## Правила

- **После каждого шага — его проверка.** Не прошла — не идти дальше, разобрать по таблице в конце, сказать
  человеку, что не так.
- **Спрашивать, а не угадывать:** где лежат проекты, какие у них базы и какая из баз **песочница**. Роль базы
  определяет только человек: `песочница` разрешает скриптам подключаться к базе, `боевая` — нет.
- **Останавливаться и отдавать человеку:**
  - установка программ с правами администратора (Docker Desktop, регистрация `comcntr.dll`);
  - пароли: не спрашивать их в чате и не вписывать в файлы. Логин базы берётся из `.dev.env` проекта
    (`dev_env` у базы) либо человек сам вписывает `logins` в `projects.local.yaml`;
  - одобрение MCP-серверов в клиенте (`claude` спросит при запуске; в Cursor — настройки MCP).
- `projects.yaml`, `projects.local.yaml`, `.mcp.json`, `.cursor/mcp.json`, `docker-compose.override.yml` в git
  не коммитить (они в `.gitignore`).
- Тексты с обратной косой (пути Windows) править инструментами редактирования файлов, а не строками Python в
  скрипте.

## 0. Что уже есть

Выполнить и записать результат:

```powershell
git --version; docker --version; docker compose version; uv --version
docker info --format "{{.ServerVersion}}"
```

- Нет `git`/`uv` — предложить установку (uv: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`),
  выполнить после согласия.
- Нет Docker или `docker info` падает — **стоп**: человек ставит/запускает Docker Desktop (WSL 2), потом продолжаем.

Уже установлен? Если есть папка с `pyproject.toml` проекта `kd2-rules-mcp` и работает
`uv run python scripts/check_server.py` — перейти к нужному шагу (новый проект — шаг 3, подключение к другому
проекту — шаг 6).

## 1. Клон и окружение

Спросить, куда ставить (по умолчанию — рядом с проектами 1С). Затем:

```powershell
git clone https://github.com/egordoronchenko/kd2-rules-mcp.git <папка>
cd <папка>
uv sync
```

Проверка: `uv run python -c "import kd2_rules_mcp; print('ok')"` → `ok`.

## 2. Какие проекты подключаем

Спросить у человека список проектов 1С (папки репозиториев) и планы обмена между ними. Для каждой папки проекта
собрать черновик **сам**, не заставляя человека искать:

- выгрузка основной конфигурации — каталог с `Configuration.xml`, где нет `ConfigurationExtensionPurpose`
  (обычно `src/Main`, `Main`, `main`); расширения — каталоги с `Configuration.xml`, в котором есть
  `<ConfigurationExtensionPurpose>` (порядок наложения и какие активны в базе — **спросить**);
- `.mcp.json` проекта: имена серверов поиска по коду (`code_mcp`), сервер данных базы, если есть (`data_mcp`);
- `.dev.env` проекта (если есть): **только имена ключей и путь к базе** — `INFOBASE_PATH`, есть ли `IB_USER`.
  Значения паролей не читать вслух и не переносить никуда.
- папка живых правил обмена (`rules_dir`) — **спросить**, держать ли правила, загружаемые в базу из файла, в
  репозитории проекта и в какой папке; только в неё сервер сможет писать. Папку создаёт человек (или агент с
  его согласия) до `setup_local.py`.

## 3. `projects.yaml`

```powershell
copy projects.example.yaml projects.yaml
```

Заполнить по черновику шага 2 (формат и поля — комментарии в `projects.example.yaml`, таблица — `docs/INSTALL.md`,
шаг 3). Пути выгрузок — **от папки проекта**. Базы: строка соединения `Srvr="…";Ref="…";` или `File="…";`, роль —
как сказал человек; `dev_env: .dev.env` (путь от папки проекта), если логин базы в нём. Показать человеку итог и
получить «да».

Проверка:

```powershell
uv run python -c "from pathlib import Path; from kd2_rules_mcp.projects import load_catalog; print(list(load_catalog(Path('projects.yaml')).projects))"
```

## 4. `projects.local.yaml`

```powershell
copy projects.local.example.yaml projects.local.yaml
```

В `projects:` — папки проектов на этой машине (ключи — как в `projects.yaml`). Остальное оставить.

## 5. Запуск

```powershell
uv run python scripts/setup_local.py
docker compose up -d --build
uv run python scripts/check_server.py
```

Проверка: `check_server.py` печатает `29 инструментов` (или больше) и «папка видна» у каждого проекта; код выхода 0.
Первая сборка образа — пара минут.

## 6. Подключить агента

- **В папке этого репозитория** `.mcp.json` и `.cursor/mcp.json` уже записал `setup_local.py`. Сказать человеку:
  перезапустить `claude` в папке и разрешить серверы (проверка — `claude mcp list`: `kd2-rules-mcp … Connected`),
  в Cursor — включить сервер в настройках MCP.
- **В папке другого проекта** (спросить, нужно ли): добавить в `.mcp.json` проекта в `mcpServers`
  `"kd2-rules-mcp": {"type": "http", "url": "<server_url из projects.local.yaml>"}`, в `.cursor/mcp.json` — то
  же без `"type"`; не затирать существующие серверы. Скопировать скиллы `kd2-rules-build` (папкой целиком,
  со справочниками `references/`) и `kd2-exchange-pitfalls` в `.claude/skills/` (и `.cursor/skills/`) проекта —
  это правка чужого репозитория,
  спросить. Проект должен быть в `projects.local.yaml` (шаги 4–5).

Итоговая проверка — вызвать инструменты сервера: `project_list`, затем `structure_load_project` для одного проекта.
Ответ с числом объектов — установка готова. Первая загрузка большой конфигурации — до пары минут.

## 7. Необязательно: база КД для `kd_check`

Только если человек хочет финальную проверку штатной загрузкой КД. Нужны платформа 1С 8.3 и `1Cv8.cf`
«Конвертации данных» 2.1 (поставка с ИТС) — путь спросить.

```powershell
& "<путь к 1cv8.exe>" CREATEINFOBASE File="<папка репозитория>\base" /UseTemplate "<путь к 1Cv8.cf>"
uv run python kdbase/kd_check.py prepare
uv run python kdbase/kd_check.py check tests/data/exchange_rules.xml
```

Проверка: `prepare` → «пользователь «Агент» готов»; `check` → `ИТОГ OK` и `КОНЕЦ`. База `base\` — только для
проверок, в git не коммитить.

## 8. Необязательно: `bsp_check`

Нужна база роли `песочница` с логином (`dev_env` или `logins`). Проверка на правилах, выгруженных из базы:
`uv run python kdbase/bsp_check.py <ExchangeRules.xml> <CorrespondentExchangeRules.xml> --plan <план> --project <проект> --base <песочница>`
→ `ИТОГ OK`. «Недопустимая строка с указанием класса» — **стоп**: человек от администратора регистрирует
`comcntr.dll` платформы (`regsvr32 "<каталог платформы>\bin\comcntr.dll"`).

## Если не получилось

| Шаг | Симптом | Действие |
|---|---|---|
| 0 | `docker info` падает | стоп: человек запускает Docker Desktop |
| 3 | `ProjectConfigError` | текст называет поле и проект — исправить `projects.yaml`, показать человеку |
| 5 | «Нет projects.yaml» / «Нет папок проектов» | шаги 3–4; путь — папка проекта, не выгрузки |
| 5 | на месте `projects.yaml` каталог | `docker compose down`, удалить каталог, шаги 3–5 |
| 5 | `check_server.py`: недоступен (код 2) | `docker compose ps`, `docker compose logs kd2-rules-mcp --tail 30`; занят порт 8060 — сказать человеку |
| 5 | «папка НЕ видна» | проект не в `projects.local.yaml` или не перезапускали `setup_local.py` + `docker compose up -d` |
| 6 | `structure_load_project`: «Нет каталога выгрузки» | поправить `dump`/`extensions` в `projects.yaml` (сервер перечитывает его при каждом вызове; если видит старое — редактор подменил файл: `docker compose restart`) |
| 7 | `prepare`: «База КД версии X, конфигурация Y» | база не из свежего шаблона — человек открывает её в 1С один раз |
| 8 | «Неверно указан пользователь или пароль» | логин базы: `dev_env` или `logins` — вписывает человек |
