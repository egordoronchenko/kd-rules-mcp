# Упаковки знаний для разных клиентов MCP

Дата: 01.10.2026. Все страницы документации прочитаны 01.10.2026; цитаты — короткие, по-английски, как в
источнике. Вопрос: как одни и те же тексты — скиллы `kd2-*`, правило работы с серверами MCP для 1С
(`docs/rules/mcp-1c.md`), справочники `docs/` — доставить в разные клиенты так, чтобы источник был один, а копии
собирались скриптом. Знания — только `.md`-файлами: доставка через ресурсы и подсказки MCP не рассматривается
(правка знаний не должна требовать пересборки контейнера). Правила набора `ai_rules_1c` в проектах 1С
(`.cursor/rules/mcp-policy.mdc`, `.cursor/rules/mcp-first-search.mdc`, манифест `.ai-rules.json`) не копируются:
упаковки ссылаются на них по именам файлов в папке проекта, а сам набор — предусловие установки.

## 1. Claude Code

| Что читает | Откуда | Когда грузится |
|---|---|---|
| Скиллы `SKILL.md` | `.claude/skills/<имя>/` проекта, `~/.claude/skills/`, вложенные `<подпапка>/.claude/skills/`, `skills/` плагина | список «имя + описание» — в каждой сессии; тело — при вызове; файлы рядом (`references/`) — когда скилл на них сошлётся [1] |
| Инструкции проекта | `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` от корня до рабочей папки; `AGENTS.md` — если `CLAUDE.md` нет; `@путь` — импорт файла | при старте; файлы подпапок — когда агент читает файлы там [2] |
| Правила `.claude/rules/*.md` | проект и `~/.claude/rules/`; из фронтматтера читается только `paths` | без `paths` — при старте; с `paths` — при чтении подходящего файла [2] |
| Субагенты | `.claude/agents/`, `~/.claude/agents/`, `agents/` плагина | по описанию или явно [3] |

- Фронтматтер скилла: все поля необязательны; `name` (по умолчанию — имя папки), `description`, `when_to_use`,
  `disable-model-invocation`, `allowed-tools`, `disallowed-tools`, `paths`, `model` и др. [1]
- Бюджет описаний: «The budget scales at 1% of the model's context window»; при переполнении отбрасываются
  описания реже вызываемых скиллов; `description` + `when_to_use` одного скилла обрезаются до 1 536 символов [1].
- `AGENTS.md` читается сам только при отсутствии `CLAUDE.md`/`CLAUDE.local.md`; иначе — `@AGENTS.md` в
  `CLAUDE.md` (так сделано в этом репозитории). Каталог `.agents/` Claude Code **не читает** [2].
- Плагин (необязательная надстройка): каталог с `.claude-plugin/plugin.json`, скиллы, агенты, хуки, `.mcp.json`;
  в адресе сервера — `${VAR:-default}` и `${user_config.KEY}`; `CLAUDE.md` в корне плагина контекстом не
  грузится («To include instructions that load into Claude's context, put them in a skill»); манифест
  необязателен, обязательное поле — только `name`; каталог — `.claude-plugin/marketplace.json` [4], [5].

## 2. Cursor (редактор и CLI)

| Что читает | Откуда | Когда грузится |
|---|---|---|
| Скиллы `SKILL.md` | `.agents/skills/`, `.cursor/skills/`, `~/.agents/skills/`, `~/.cursor/skills/`; «for backward compatibility» — `.claude/skills/`, `.codex/skills/`, `~/.claude/skills/`, `~/.codex/skills/` | описание — всегда; остальное — по мере надобности [6] |
| Правила `.cursor/rules/*.mdc` | проект; `.md` без фронтматтера игнорируются | `alwaysApply: true` — всегда; `alwaysApply: false` + `description` — по решению агента; + `globs` — при подходящем файле; без обоих — только `@`-упоминанием [7] |
| `AGENTS.md` | корень и вложенные папки (вложенные объединяются с родительскими) | как правило проекта [7] |
| Субагенты | `.cursor/agents/`, `.claude/agents/`, `.codex/agents/` и те же в `~` | по описанию, `/имя` или словами [8] |

- Фронтматтер скилла: обязательны `name` (строчные латинские, цифры, дефис) и `description`; необязательны
  `paths`, `disable-model-invocation`, `icon`, `color`, `metadata` [6].
- **CLI** (`cursor-agent`): «The CLI agent supports the same rules system as the editor»; читает `AGENTS.md` и
  `CLAUDE.md` в корне проекта «alongside `.cursor/rules`» и `mcp.json`; скиллы выбираются из меню `/` — грузит ли
  CLI их описания сам, страница прямо не говорит (не проверено) [9].
- Субагенты: поля `name`, `description`, `model` (`inherit` по умолчанию), `readonly`, `is_background`;
  инструменты наследуются от родителя, включая MCP, — поля списка инструментов нет, ограничение только
  `readonly` [8].
- Распространение: скиллы «aren't imported independently from repositories» — из GitHub только плагином через
  marketplace; у плагинов свой манифест (`.cursor-plugin/plugin.json` или корневой `plugin.json` открытого
  стандарта Agent Plugins); о чтении плагинов Claude Code документация молчит [6], [10].

**Следствие:** Cursor читает `.claude/skills/` сам. Ручная копия `.cursor/skills/kd2-*` в этом репозитории не нужна
и, вероятно, даёт агенту Cursor каждый скилл дважды (как Cursor разрешает одинаковые имена из двух каталогов, в
документации не сказано — проверить).

## 3. Прочие клиенты с `AGENTS.md`

| Клиент | Инструкции | Скиллы | Когда грузится |
|---|---|---|---|
| Codex | `~/.codex/AGENTS.md` (или `.override.md`), затем `AGENTS.override.md`/`AGENTS.md` от корня git до рабочей папки; лимит `project_doc_max_bytes` 32 КиБ; другие имена — `project_doc_fallback_filenames` [11] | `.agents/skills` в каждой папке от рабочей до корня репозитория, `$HOME/.agents/skills`, `/etc/codex/skills`; обязательны `name`, `description` [12] | инструкции — при старте; список скиллов — не больше 2 % окна контекста (8 000 символов, если окно неизвестно), тело — при выборе скилла [12] |
| OpenCode | `AGENTS.md` (вверх от рабочей папки), запасной — `CLAUDE.md`; глобально `~/.config/opencode/AGENTS.md`; поле `instructions` в `opencode.json` — пути, маски, URL [13] | `.opencode/skills/`, `.claude/skills/`, `.agents/skills/` и те же в `~`; поля `name`, `description`, `license`, `compatibility`, `metadata` [14] | ссылки на файлы внутри `AGENTS.md` сами не разбираются: «opencode doesn't automatically parse file references» — нужен `instructions` или прямое указание прочитать [13] |

Переносимый файл инструкций `KD-RULES.md` (упаковка для клиентов без скиллов или как точка входа) должен:
быть самодостаточным и коротким (≤ 60 строк — в лимит Codex помещается с запасом); давать порядок работы
(структуры → правила → кандидаты → правки → проверки → сохранение) и **прямые указания** «прочитай
`kd2-rules/<файл>.md`, когда …» — OpenCode и, вероятно, другие клиенты ссылки сами не открывают; называть точки
остановки (пароли, правка чужого репозитория, загрузка в небезопасную базу); называть серверы 1С ролями и
отсылать к `project_list`; ссылаться на правила набора `ai_rules_1c` проекта по путям `.cursor/rules/…`.
Подключение — одна строка в `AGENTS.md` пользователя: «Правила обмена КД 2 и сервер kd-rules-mcp — прочитай
`KD-RULES.md` перед работой с ними» (для Claude Code — `@KD-RULES.md` в `CLAUDE.md`, для OpenCode — путь в
`instructions`).

## 4. Субагенты для роли «разработчик обработчиков»

| | Claude Code [3] | Cursor [8] |
|---|---|---|
| Где | `.claude/agents/*.md`, `~/.claude/agents/`, плагин | `.cursor/agents/`, **`.claude/agents/`**, `.codex/agents/` и в `~` |
| Обязательные поля | `name`, `description` | `name`, `description` |
| Модель | `model` (`sonnet`, `opus`, `haiku`, `fable` или ID) | `model` (`inherit` или ID) |
| Ограничение инструментов | `tools` (разрешённые), `disallowedTools` (запрещённые); MCP — `mcp__<сервер>` или `mcp__<сервер>__*` | нет; только `readonly: true` |
| Прочее | `skills` — заранее загрузить скиллы; `omitClaudeMd` | `is_background` |

Один файл `.claude/agents/kd2-handler-developer.md` виден обоим клиентам. Запрет `rules_save`, `kdbase` и
оболочки работает в Claude Code полем `disallowedTools` (например, `Bash, mcp__kd-rules-mcp__rules_save`); в
Cursor — только текстом роли. Читает ли Cursor поля Claude Code (`tools`, `skills`) или игнорирует их, в
документации не сказано — проверить на одном файле до того, как заводить вторую копию в `.cursor/agents/`.

## 5. Рекомендация

**Источник правды** — нейтральные тексты в репозитории сервера:

```
skills/kd2-rules-build/         SKILL.md + references/
skills/kd2-exchange-pitfalls/   SKILL.md (+ references/)
skills/kd-install/             SKILL.md
skills/KD-RULES.md             точка входа для клиентов без скиллов
docs/rules/mcp-1c.md            правило работы с серверами 1С
docs/checks.md, docs/tools.md, docs/glossary.md   справочники, входят в упаковки копиями
```

**Сборка** — `scripts/build_packs.py --client claude|agents|cursor-rules [--dest <папка>] [--check]` и тест
`--check` в pytest:

| Упаковка | Что кладёт | Кто её читает |
|---|---|---|
| `claude` | `.claude/skills/kd2-*` (+ `references/mcp-1c.md`, `checks.md`, `tools.md`, `glossary.md` в скиллы, которые на них ссылаются) | Claude Code, Cursor, OpenCode |
| `agents` | `.agents/skills/kd2-*` (то же) + `KD-RULES.md` | Codex, Cursor, OpenCode |
| `cursor-rules` | `.cursor/rules/mcp-1c.mdc` полным текстом с `description` и `alwaysApply: false` | Cursor (редактор и CLI) |

- В один проект ставится **либо** `claude`, **либо** `agents`: Cursor и OpenCode читают оба каталога и получат
  каждый скилл дважды. Есть Claude Code — `claude`; нет — `agents`. `cursor-rules` — добавкой, если пользуются
  Cursor. Тест «чужой проект» собирает каждую упаковку в пустую папку и проверяет: ссылки разрешаются внутри
  упаковки, путей этого репозитория (`scripts/`, `workspace\`, `docs/`) нет, имена инструментов есть в сервере.
- **Исчезают:** `.cursor/skills/kd2-*` (ручная копия) и побайтовое сравнение копий в `tests/test_skills.py` (его
  место — `build_packs.py --check`); шаг 6 `kd-install`/`INSTALL.md` «скопировать скиллы в проект» превращается в
  `build_packs.py --client … --dest <папка проекта>` с согласия человека. **Появляются:** `skills/`,
  `scripts/build_packs.py`, `.agents/skills/kd2-*` — только в чужих проектах по `--dest`, в этом репозитории их
  не коммитить (тут уже лежат `.agents/skills/openspec-*`, и Cursor увидел бы `kd2-*` дважды).
- `.cursor/rules/mcp-1c.mdc` из указателя на `docs/rules/` становится сборкой полного текста.
- `setup_local.py` не меняется по знаниям: он пишет `.mcp.json` и `.cursor/mcp.json` этого репозитория. Для
  чужого проекта запись `kd-rules-mcp` в его `.mcp.json` и `.cursor/mcp.json` — часть `build_packs.py --dest`
  (добавить, не затирая существующие серверы; адрес — `server_url` из `projects.local.yaml`).
- **Имена серверов 1С** в упаковках — ролями («сервер кода источника», «сервер данных песочницы приёмника»); имя
  агент берёт из `project_list` и списка подключённых серверов своего клиента. Шаблон `<проект>-1c-…` верен
  только для папки этого репозитория (там серверы подключает `setup_local.py` с префиксом проекта); в папке
  проекта 1С серверы называются так, как в его `.mcp.json`.
- Плагин Claude Code и плагин Cursor — необязательные упаковки поверх `skills/`, когда установка копированием
  окажется тяжелее одной команды; источник от этого не меняется.

**Не проверено в этой работе:** как Cursor разрешает одинаковые имена скиллов из `.claude/skills` и
`.cursor/skills`; грузит ли `cursor-agent` описания скиллов без меню `/`; читает ли Cursor поля `tools`/`skills`
субагентов Claude Code; поведение Codex на ссылках из `AGENTS.md` (документация описывает только склейку файлов).

## Источники

1. https://code.claude.com/docs/en/skills
2. https://code.claude.com/docs/en/memory
3. https://code.claude.com/docs/en/sub-agents
4. https://code.claude.com/docs/en/plugins
5. https://code.claude.com/docs/en/plugins/manifest-reference
6. https://cursor.com/docs/context/skills
7. https://cursor.com/docs/context/rules
8. https://cursor.com/docs/context/subagents
9. https://cursor.com/docs/cli/using
10. https://cursor.com/docs/plugins
11. https://learn.chatgpt.com/docs/agent-configuration/agents-md (адрес `developers.openai.com/codex/guides/agents-md` перенаправляет сюда)
12. https://learn.chatgpt.com/docs/build-skills (перенаправление с `developers.openai.com/codex/skills`)
13. https://opencode.ai/docs/rules/
14. https://opencode.ai/docs/skills/
