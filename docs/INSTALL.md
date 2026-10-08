# Установка kd-rules-mcp — пошагово

Инструкция для человека. Агенту дайте скилл установки: «Установи kd-rules-mcp по
https://github.com/egordoronchenko/kd-rules-mcp/blob/main/.claude/skills/kd-install/SKILL.md» — он проведёт
по тем же шагам, спросит нужное и остановится там, где нужны вы: программы с правами администратора, пароли,
согласие с `projects.yaml`, правка репозитория проекта 1С, перезапуск клиента и одобрение сервера.

После каждого шага есть **проверка** — что должно получиться. Не получилось — раздел «Если не получилось» в конце.

Два варианта:

- **Минимальный** (шаги 1–6) — сервер в Docker, агент разбирает, правит и проверяет правила по XML-выгрузкам.
  Хватает для работы.
- **Полный** (плюс шаги 7–8) — финальные проверки файла штатной загрузкой: через базу «Конвертации данных» и
  через загрузку правил БСП в базе-песочнице. Нужны Windows и платформа 1С 8.3.

Обновление уже установленного сервера и откат — шаг 9.

## Готовый образ без клона

Это первый вариант установки: нужен только Docker с Compose и клиент агента. Git, Python и uv
на машине не требуются. Сначала выберите в [релизах](https://github.com/egordoronchenko/kd-rules-mcp/releases)
тег с опубликованным образом: `vX.Y.Z` ниже — заменяемый пример, образ имеет тег `X.Y.Z` без `v`.
Архитектуры — `linux/amd64` и `linux/arm64` (Windows запускает Linux-контейнеры через Docker Desktop).
При первой публикации принимающий проверяет доступность пакета GHCR без авторизации;
для установки на чистой машине его видимость должна быть public.

Быстрый старт — скачать три файла тега, заполнить настройки, выполнить `setup` и `docker compose up -d`.
Исходники для скачивания:
[docker-compose.yml](https://raw.githubusercontent.com/egordoronchenko/kd-rules-mcp/vX.Y.Z/docker-compose.yml),
[projects.example.yaml](https://raw.githubusercontent.com/egordoronchenko/kd-rules-mcp/vX.Y.Z/projects.example.yaml),
[projects.local.example.yaml](https://raw.githubusercontent.com/egordoronchenko/kd-rules-mcp/vX.Y.Z/projects.local.example.yaml).
Ссылки тоже требуют выбранного тега вместо `vX.Y.Z`.

### Windows PowerShell: установка и проверка на чистой машине

В новой папке (команды совместимы с Windows PowerShell 5.1):

```powershell
New-Item -ItemType Directory kd-rules-mcp-clean | Out-Null
Set-Location kd-rules-mcp-clean
$releaseTag = 'vX.Y.Z' # выбранный опубликованный тег с образом
$kdVersion = $releaseTag.Substring(1)
docker --version
docker compose version
docker info --format '{{.ServerVersion}}'
'docker-compose.yml','projects.example.yaml','projects.local.example.yaml' | ForEach-Object { Invoke-WebRequest "https://raw.githubusercontent.com/egordoronchenko/kd-rules-mcp/$releaseTag/$_" -OutFile $_ }
Copy-Item projects.example.yaml projects.yaml
Copy-Item projects.local.example.yaml projects.local.yaml
```

Заполните `projects.yaml` по шагу 3 и `projects.local.yaml` по шагу 4 ниже: абсолютные пути
к существующим проектам с XML-выгрузками; удалите проекты примера, которых у вас нет. Впишите
`image_tag: X.Y.Z` с выбранной версией. Для этой минимальной проверки оставьте стандартные
`server_url` и порт, без `bind`/`token`. Каталоги `rules_dir` и `writable_extensions`, если они
описаны, должны существовать до запуска compose. Затем:

```powershell
docker run --rm -v "${PWD}:/work" "ghcr.io/egordoronchenko/kd-rules-mcp:$kdVersion" setup
if ($LASTEXITCODE -ne 0) { throw 'setup завершился с ошибкой' }
'.env','docker-compose.override.yml','.mcp.json','.cursor/mcp.json' | ForEach-Object { if (-not (Test-Path -LiteralPath $_ -PathType Leaf)) { throw "Нет $_" } }
if (Test-Path .git) { throw 'Проверка должна проходить без клона' }
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Некорректный compose' }
docker compose pull
if ($LASTEXITCODE -ne 0) { throw 'Образ не скачан' }
docker compose up -d --no-build
if ($LASTEXITCODE -ne 0) { throw 'Сервер не запущен' }
docker compose ps
docker compose exec -T kd-rules-mcp python scripts/check_server.py http://127.0.0.1:8060/mcp
if ($LASTEXITCODE -ne 0) { throw 'Проверка сервера не прошла; проверьте logs и повторите после готовности сервера' }
```

Для Windows cmd эквивалент настройки:
`docker run --rm -v "%cd%":/work ghcr.io/egordoronchenko/kd-rules-mcp:X.Y.Z setup`.

### sh: установка и проверка на чистой машине

```sh
set -eu
mkdir kd-rules-mcp-clean
cd kd-rules-mcp-clean
release_tag=vX.Y.Z # выбранный опубликованный тег с образом
kd_version=${release_tag#v}
docker --version
docker compose version
docker info --format '{{.ServerVersion}}'
for file in docker-compose.yml projects.example.yaml projects.local.example.yaml; do
  curl -fsSL "https://raw.githubusercontent.com/egordoronchenko/kd-rules-mcp/$release_tag/$file" -o "$file"
done
cp projects.example.yaml projects.yaml
cp projects.local.example.yaml projects.local.yaml
```

Заполните оба файла как выше, в `projects.local.yaml` укажите абсолютные пути Linux/macOS и
`image_tag: X.Y.Z`. После заполнения:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$(pwd):/work" "ghcr.io/egordoronchenko/kd-rules-mcp:$kd_version" setup
for file in .env docker-compose.override.yml .mcp.json .cursor/mcp.json; do test -f "$file"; done
test ! -d .git
if [ "$(uname -s)" = Darwin ]; then kd_owner=$(stat -f %u .env); else kd_owner=$(stat -c %u .env); fi
test "$kd_owner" = "$(id -u)"
docker compose config --quiet
docker compose pull
docker compose up -d --no-build
docker compose ps
docker compose exec -T kd-rules-mcp python scripts/check_server.py http://127.0.0.1:8060/mcp
```

Без `--user` настройка на Linux пишет файлы владельца root и предупреждает. `setup` не меняет
владельца тома `/work`; с `--user` пишет от указанного uid/gid. Сервер при обычном запуске работает
от uid 1000. Если он не может писать в `workspace`, дайте этому uid доступ к рабочей папке.
`check_server.py` запускается **в уже работающем контейнере сервера**, поэтому его loopback —
именно сервер. Отдельной подкоманды `check` в одноразовом контейнере нет. Проверка выше для
локальной установки без токена; проверка авторизованного MCP — клиентом после одобрения сервера.

Настройка использует тот же код, что `scripts/setup_local.py`. `.env` содержит `KD_IMAGE_TAG`
только при заданном `image_tag`; без него compose использует `latest`. Пути проектов, рабочей
папки и каталогов на запись остаются **путями хоста**, а стандартные `workspace` и `structures`
в карте путей относительны папке compose. Для работы агента из другой папки задайте `workspace`
абсолютным путём в `projects.local.yaml`. Внешние каталоги не видны запуску только с `/work`:
`setup` предупреждает об этом, а каталоги на запись берёт из описания проектов. Чтобы также
собрать серверы 1С из `.mcp.json` и логины из `.dev.env`, подключите проект только для чтения и
передайте его идентификатор (повторите для каждого нужного проекта):

```powershell
docker run --rm -v "${PWD}:/work" -v "D:/Repos/bp:/inputs/bp:ro" "ghcr.io/egordoronchenko/kd-rules-mcp:$kdVersion" setup --project-mount bp=/inputs/bp
```

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$(pwd):/work" -v "/srv/repos/bp:/inputs/bp:ro" "ghcr.io/egordoronchenko/kd-rules-mcp:$kd_version" setup --project-mount bp=/inputs/bp
```

Скиллы скачайте из того же релиза: `kd-rules-mcp-skills-claude-X.Y.Z.zip` для Claude Code,
Cursor/OpenCode или `kd-rules-mcp-skills-agents-X.Y.Z.zip` для Codex. Для проверки ZIP без клона:

```powershell
Invoke-WebRequest "https://github.com/egordoronchenko/kd-rules-mcp/releases/download/$releaseTag/kd-rules-mcp-skills-agents-$kdVersion.zip" -OutFile skills.zip
Expand-Archive skills.zip -DestinationPath skills-check
if (-not (Test-Path skills-check/.agents/skills/kd3-rules/SKILL.md)) { throw 'Нет скилла КД 3' }
if (-not (Test-Path skills-check/KD-RULES.md)) { throw 'Нет точки входа Codex' }
```

```sh
curl -fsSL "https://github.com/egordoronchenko/kd-rules-mcp/releases/download/$release_tag/kd-rules-mcp-skills-agents-$kd_version.zip" -o skills.zip
unzip -q skills.zip -d skills-check
test -f skills-check/.agents/skills/kd3-rules/SKILL.md
test -f skills-check/KD-RULES.md
```

Установите **одну** упаковку в проект: скопируйте её `.claude/skills` или `.agents/skills` и
`KD-RULES.md` (для agents). В существующем `.mcp.json` объедините только запись `kd-rules-mcp`
с текущими серверами, укажите свой адрес и заголовок при токене; не заменяйте весь файл из ZIP.
Для Cursor добавьте запись также в `.cursor/mcp.json`. Подключение клиента и первая загрузка
структуры — шаг 6 ниже. В новом сеансе агент должен выполнить `project_list` и
`structure_load_project`: это финальная проверка метаданных после проверки доступности сервера.

## Установка из исходников (альтернатива)

Этот путь нужен для разработки или дополнительных проверок через базы 1С; в нём нужны Git и uv.

---

## 1. Программы

| Что | Зачем | Где взять |
|---|---|---|
| Git | скачать репозиторий | [git-scm.com](https://git-scm.com/download/win) |
| Docker Desktop | запуск сервера | [docker.com](https://www.docker.com/products/docker-desktop/) — при установке оставьте WSL 2 |
| uv | Python-окружение для скриптов настройки и проверок (Python 3.12 он скачает сам) | в PowerShell: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Claude Code или Cursor | агент, который работает с сервером | [claude.com/claude-code](https://claude.com/claude-code), [cursor.com](https://cursor.com) |

**Проверка** — в новом окне PowerShell все четыре команды отвечают версией:

```powershell
git --version
docker --version
docker compose version
uv --version
```

Docker Desktop должен быть запущен (значок кита в трее, статус «Engine running»).

## 2. Репозиторий

```powershell
cd D:\Tools                       # любая папка
git clone https://github.com/egordoronchenko/kd-rules-mcp.git
cd kd-rules-mcp
uv sync
```

**Проверка:** `uv run python -c "import kd_rules_mcp; print('ok')"` печатает `ok`.

## 3. Описать проекты — `projects.yaml`

Сервер работает с **XML-выгрузками конфигураций** — теми, что Конфигуратор делает командой «Конфигурация →
Выгрузить конфигурацию в файлы» (формат Конфигуратора, не EDT). Обычно они уже лежат в git-репозитории проекта.

```powershell
copy projects.example.yaml projects.yaml
```

Откройте `projects.yaml` и опишите свои проекты. На каждый проект:

| Поле | Что писать | Как узнать |
|---|---|---|
| ключ проекта (`bp:`) | короткое латинское имя; из него получится имя структуры `bp-full` | придумать |
| `name` | название для людей | — |
| `configurations.full.dump` | путь к выгрузке основной конфигурации **от папки проекта** | папка, где лежит `Configuration.xml` основной конфигурации, например `src/Main` |
| `configurations.full.extensions` | выгрузки расширений в порядке наложения, только активные в базе | папки с `Configuration.xml` каждого расширения |
| `configurations.full.writable_extensions` | необязательно: пути из `extensions`, которые `setup_local.py` подключает на запись для доставки в своё расширение | например, `[src/cfe/МоёРасширение]` |
| `bases` | базы проекта: `role` — `песочница` (скриптам можно подключаться) или `боевая` (только для справки), `connection` — строка соединения | `Srvr="сервер";Ref="база";` для серверной, `File="D:\Bases\база";` для файловой |
| `bases.*.dev_env` | необязательно: `.dev.env` проекта с `IB_USER`/`IB_PASSWORD` этой базы | если логин базы уже лежит в `.dev.env` проекта |
| `bases.*.data_mcp` | необязательно: имя MCP-сервера данных этой базы из `.mcp.json` проекта | если в базе опубликован MCP (см. README, «С чем работает в паре») |
| `mcp_config`, `code_mcp` | необязательно: `.mcp.json` проекта и имена серверов поиска по коду в нём | если проект подключён к MCP-серверам для 1С |
| `rules_dir` | необязательно: папка живых правил обмена **от папки проекта** — сервер сможет сохранять туда | если правила загружаются в базу из файла и вы хотите держать их в git проекта; папку создать заранее |
| `exchanges` | план обмена и проекты, между которыми он работает | имя плана обмена из конфигурации |

Паролей и путей вашей машины в `projects.yaml` нет — его можно отдать коллегам.

**Что должно быть в проекте.** Проверьте до шага 5 — иначе ошибки всплывут позже:

| Что | Где видно | Без него |
|---|---|---|
| XML-выгрузка конфигурации и расширений (формат Конфигуратора, не EDT) | каталоги с `Configuration.xml` | сервер не работает с проектом |
| база-песочница | строка соединения в `bases` | нет проверок `bsp_check` и `exchange_check` (шаг 8) |
| MCP-серверы для 1С (пакет comol, README «С чем работает в паре»): код и метаданные, данные базы | `.mcp.json` проекта | правила разбираются и правятся, но код обработчиков агент пишет без API конфигурации |
| набор правил comol [`ai_rules_1c`](https://github.com/comol/ai_rules_1c) | `.ai-rules.json` в корне проекта, правила `.cursor\rules\mcp-policy.mdc` и `.cursor\rules\mcp-first-search.mdc` (для Claude Code — `.claude\rules-1c\`, для Codex — `.codex\rules\`) | серверы 1С работают хуже: скиллы kd-rules-mcp опираются на эти правила, а не повторяют их |

**Проверка:**

```powershell
uv run python -c "from pathlib import Path; from kd_rules_mcp.projects import load_catalog; print(list(load_catalog(Path('projects.yaml')).projects))"
```

печатает список ваших проектов. Ошибка говорит, какое поле и в каком проекте не так.

## 4. Настройки машины — `projects.local.yaml`

```powershell
copy projects.local.example.yaml projects.local.yaml
```

В `projects:` укажите, где на этой машине лежит папка каждого проекта (любые диски). Проекты, которых у вас нет,
не указывайте. Остальное можно оставить как в примере.

Сервер для команды, не только для этой машины: в том же файле `bind` (адрес интерфейса, например `0.0.0.0` или
адрес машины) и `token`. `setup_local.py` опубликует порт на этот интерфейс (`KD2_BIND` в `.env`) и запишет
заголовок `Authorization: Bearer` в `.mcp.json`. Без `token` на внешнем интерфейсе (не `127.0.0.1`, не
`localhost`, не `::1`) скрипт откажется. На своей машине оба поля не задавайте.

Порт 8060 занят — в том же файле `port` (публикуемый порт, внутри контейнера по-прежнему 8060) и тот же порт в
`server_url`. Иначе скрипт предупредит, что адрес клиента и публикация не совпали. Второй экземпляр сервера на
этой машине — `instance` (латиница, цифры, `_` и `-`): у контейнера, тома кэша и проекта compose будут свои
имена, том первого экземпляра не затронут. Пример в `projects.local.example.yaml`: `port: 8061`, `instance: stand`.
Без `instance` второй экземпляр в другой папке получит те же имена по умолчанию и **пересоздаст первый контейнер**
без его монтирований — так случилось при проверке 1.1.0; всегда задавайте `instance` второму экземпляру.

Логин базы, если его нет в `.dev.env` проекта, — в `logins:` этого файла (он в git не попадает).

## 5. Запуск

```powershell
uv run python scripts/setup_local.py
docker compose up -d --build
```

`setup_local.py` пишет файлы (в git они не попадают): `.env` — интерфейс, порт и имена второго экземпляра, если
заданы `bind`, `port` или `instance` (иначе файла нет и действуют умолчания compose), `docker-compose.override.yml`
— какие папки проектов контейнер видит (только на чтение) и `KD2_TOKEN`, `.mcp.json` — подключение для Claude
Code, `.cursor/mcp.json` — для Cursor. Первая сборка образа — пара минут.

Порт по умолчанию виден только своей машине (`127.0.0.1:8060`), контейнер `kd_rules_mcp`, том
`kd2_structures_cache`. С других машин до него не достучаться. Сервер для команды — `bind` и `token` (шаг 4).
Другой порт — `port` и тот же порт в `server_url`. Второй экземпляр — `instance` (и свой `port`, если 8060 занят
первым). Затем снова `setup_local.py` и `docker compose up -d`. Скрипт печатает имя контейнера, публикацию
`адрес:порт` и имя тома — их же показывает `docker compose ps`.

**Проверка:**

```powershell
docker compose ps                 # kd_rules_mcp — running
uv run python scripts/check_server.py
```

`check_server.py` подключается к серверу как агент и печатает число инструментов и ваши проекты с отметкой,
видна ли их папка:

```
Сервер http://localhost:8060/mcp: N инструментов
  bp: папка видна, конфигурации: full
  zup: папка НЕ видна — проверьте projects.local.yaml, setup_local.py, docker compose up -d
```

Число инструментов растёт с версиями; полный список — [tools.md](tools.md).

## 6. Подключить агента

**Claude Code.** Запустите `claude` в папке репозитория — он спросит, разрешить ли серверы из `.mcp.json`,
ответьте да. Проверка: `claude mcp list` показывает `kd-rules-mcp … ✓ Connected`.

**Cursor.** Откройте папку репозитория, Settings → MCP — включите `kd-rules-mcp`. Проверка: у сервера зелёная
точка и список инструментов.

Если задан `token`, `setup_local.py` добавляет в запись `kd-rules-mcp` в `.mcp.json` и `.cursor/mcp.json`
заголовок `Authorization: Bearer <token>`. Без него клиент получит HTTP 401.

**Работать из папки проекта 1С** — поставьте туда скиллы и подключение сервера (это правка репозитория
проекта):

```powershell
uv run python scripts/build_packs.py --dest <папка проекта> --client claude
```

Скрипт кладёт скиллы сервера (имена `kd-`, `kd2-` или `kd3-`) в `.claude\skills\` проекта (их читают Claude Code, Cursor и OpenCode) вместе со
справочниками и правилом работы с серверами 1С, добавляет `kd-rules-mcp` в `.mcp.json` проекта (и в
`.cursor\mcp.json`, если он есть) и не трогает другие серверы. Адрес — `server_url` из `projects.local.yaml`,
другой — `--server-url`. Заголовок с токеном (если он задан в `projects.local.yaml`) скрипт переносит сам.
Работаете в Cursor — добавьте `--cursor`: скрипт создаст `.cursor\mcp.json`, если его нет, и перенесёт в него
HTTP-серверы из `.mcp.json` проекта (серверы с `command` не переносит и называет их в отчёте). Если
`.cursor\mcp.json` уже есть, `--cursor` дописывает только недостающие HTTP-серверы 1С и не меняет существующие
записи; без флага скрипт их не переносит и сообщает, каких серверов 1С в файле нет. Без флага и без этого файла
добавьте сервер сами в настройках MCP Cursor. Папка проекта должна быть в `projects.local.yaml` (шаги 4–5):
сервер видит только подключённые папки и пишет результат в свою рабочую папку, в `rules_dir` проектов
и в выгрузки расширений из `writable_extensions`.

Клиент, который `.claude\skills` не читает (например, Codex), — `--client agents`: скиллы ложатся в
`.agents\skills\`, а в корень проекта — `KD-RULES.md` (порядок работы и какой файл когда читать). Подключите
его строкой в `AGENTS.md` проекта: «Правила обмена КД 2 и сервер kd-rules-mcp — прочитай KD-RULES.md перед
работой с ними». Обе упаковки в один проект не ставятся: Cursor и OpenCode увидели бы скиллы дважды.

**Проверка:** скрипт печатает число файлов упаковки и «kd-rules-mcp добавлен»; клиент, запущенный в папке
проекта, видит сервер после одобрения.

Клиент читает `.mcp.json` только при запуске. Если файл записан, пока клиент уже работал (например, установку
вёл агент), инструментов сервера в этой сессии нет: перезапустите клиент в нужной папке и одобрите сервер. Агент
установки на этом шаге останавливается и просит об этом; сервер в обход клиента он не вызывает.

**Первая задача — проверка всей цепочки**, в новой сессии после одобрения. Попросите агента:

> Через kd-rules-mcp покажи project_list и загрузи структуру проекта `bp`.

Ответ — число объектов конфигурации (для типовой БП — несколько тысяч). Первая загрузка большой конфигурации —
до пары минут, повторная — доли секунды.

Готово: минимальная установка работает. Как ставить задачи — README, раздел 3.

---

## 7. Проверка через базу «Конвертации данных» (необязательно)

Шаги 7–8 готовят две ступени лестницы проверок — `kd_check` и `bsp_check`. Что каждая доказывает и чего нет, как
читать итог и какие замечания ложные — [checks.md](checks.md); здесь — только установка и проверка, что работает.

Нужны Windows и платформа 1С 8.3 (толстый клиент), а также конфигурация «Конвертация данных» 2.1 — поставка с
ИТС (файл `1Cv8.cf` в каталоге шаблонов после установки поставки).

1. Создайте файловую базу КД в папке `base` репозитория (из PowerShell, путь к платформе — свой):

   ```powershell
   & "C:\Program Files\1cv8\8.3.27.2170\bin\1cv8.exe" CREATEINFOBASE File="D:\Tools\kd-rules-mcp\base" /UseTemplate "<путь к 1Cv8.cf КД>"
   ```

   или обычным способом: список баз → Добавить → Создание новой базы из шаблона КД, каталог — `base` репозитория.
2. Подготовьте её (один раз):

   ```powershell
   uv run python kdbase/kd_check.py prepare
   ```

   **Проверка:** `…версия 2.1.8.2, пользователь «Агент» готов`.
3. **Проверка:** `uv run python kdbase/kd_check.py check tests/data/exchange_rules.xml` → последние строки
   `ИТОГ OK` и `КОНЕЦ` (около 10–20 с).

Платформа ищется сама (последняя в `Program Files\1cv8`); другую — `onec_platform` в `projects.local.yaml`.

## 8. Проверка загрузкой правил БСП в песочнице (необязательно)

`bsp_check` работает только с базами роли `песочница` из `projects.yaml` и ничего не записывает в базу.

1. Нужен COM-коннектор платформы. Обычно он зарегистрирован установщиком. Если скрипт ответит «Недопустимая строка
   с указанием класса», зарегистрируйте его из PowerShell **от администратора**:
   `regsvr32 "C:\Program Files\1cv8\<версия>\bin\comcntr.dll"`.
2. Логин базы — в `.dev.env` проекта (`dev_env` у базы) или в `logins` файла `projects.local.yaml`. Без логина
   к клиент-серверной базе скрипт отказывает (вход под пустым пользователем не пробует).
3. **Проверка** — на правилах, выгруженных из базы (ZIP «Сохранить правила конвертации в файл», распакуйте):

   ```powershell
   uv run python kdbase/bsp_check.py ExchangeRules.xml CorrespondentExchangeRules.xml --plan <план обмена> --project bp --base bp_dev
   ```

   → `ИТОГ OK`.

---

## 9. Обновление

Перед обновлением, до любых команд ниже (`git pull` и `uv sync` тоже), **сохраните работу агента**: открытые
на сервере проекты правил переживают пересоздание контейнера (снимки в `workspace\.projects\`), но новая версия
может сменить формат снимка. Попросите агента сохранить правила (`rules_save`) или проверьте сами:
`rules_projects` в клиенте — что открыто, `saved_path` — куда сохранено; не сохранён проект с пустым
`saved_path` или с `modified: true` (правки после открытия или сохранения).

**Готовый образ.** Запишите прежний `image_tag` (если использовали `latest`, сначала выберите тег
этой версии в релизах). Имя — `ghcr.io/egordoronchenko/kd-rules-mcp`, тег — версия без `v`.
Для обновления поменяйте `image_tag` в `projects.local.yaml`, скачайте compose и примеры нового
тега, повторите `docker run … ghcr.io/egordoronchenko/kd-rules-mcp:<новая-версия> setup`, затем
`docker compose config --quiet`, `docker compose pull` и `docker compose up -d --no-build`.
Используйте те же дополнительные тома проектов и `--user`, что при первой настройке.
Проверка — `check_server.py` через `docker compose exec`, затем инструменты в клиенте.

**Откат готового образа.** Верните `image_tag: <прежняя-версия>`, выполните `setup` из
`ghcr.io/egordoronchenko/kd-rules-mcp:<прежняя-версия>`, `docker compose pull` и
`docker compose up -d --no-build --force-recreate`; проверьте сервер. Compose и скиллы тоже
берутся из прежнего тега. Том кэша и `workspace` сохраняются.

**Из исходников.** Имя образа теперь тоже `ghcr.io/egordoronchenko/kd-rules-mcp`; текущий тег
показывает `docker compose config --images`. При `image_tag` ниже замените `latest` своим тегом.

```powershell
cd <папка kd-rules-mcp>
docker compose config --images                     # имя образа, ниже — по умолчанию
docker image tag ghcr.io/egordoronchenko/kd-rules-mcp:latest ghcr.io/egordoronchenko/kd-rules-mcp:prev
git pull
uv sync
uv run python scripts/setup_local.py
docker compose config                              # те же имя контейнера, порт и том, что были
docker compose up -d --build
uv run python scripts/check_server.py
```

- `setup_local.py` запускайте после каждого обновления: новая версия может добавить в `docker-compose.override.yml`
  и `.mcp.json` папки, переменные или серверы. Он переписывает эти файлы целиком — ручные правки в них теряются
  (они генерируются; правьте `projects*.yaml`). Свой `.env` (с заголовком «Сгенерировано scripts/setup_local.py»)
  он тоже переписывает или удаляет; `.env`, написанный не им, оставляет и предупреждает об этом.
- Перед `up` сверьте `docker compose config`: имя контейнера, порт и том должны быть те же, что у этого
  экземпляра. Если на машине два экземпляра (`instance` в `projects.local.yaml` или ручной `.env`) и имена вдруг
  стали «по умолчанию» (`kd_rules_mcp`, 8060), `up` пересоздаст чужой контейнер — остановитесь и разберитесь.
- **Кэш структур** (том `kd2_structures_cache`) переживает обновление. Если в новой версии изменился код
  загрузчика структур, сохранённая структура при следующем `structure_load_project` (или `structure_load_xml`,
  `structure_load_md83exp`) собирается заново сама — ключ кэша включает хеш кода загрузчика
  (`src/kd_rules_mcp/structures/store.py:36-37`). До повторной загрузки запросы отвечают по старой структуре,
  поэтому агент загружает структуры в начале каждой задачи.
- `projects.yaml` и `projects.local.yaml` обновление не трогает. Если в `projects.example.yaml` появились новые
  поля — сравните со своим `projects.yaml` (`git diff <старый коммит> -- projects.example.yaml`).
- Скиллы в папках проектов 1С (шаг 6) сами не обновляются — повторите `build_packs.py --dest <папка проекта>`
  с той же упаковкой: файлы обновятся, устаревшие удалятся. Повтор с `--cursor` дописывает в `.cursor\mcp.json`
  недостающие HTTP-серверы 1С, уже записанные не меняет.

**Проверка:** `docker compose ps` — `running`; `check_server.py` печатает число инструментов и «папка видна» у
проектов; в клиенте `project_list` отвечает.

**Откат.** Вернуть код и прежний образ (имя образа — как при метке `:prev`):

```powershell
git checkout <прежний коммит или тег>
docker image tag ghcr.io/egordoronchenko/kd-rules-mcp:prev ghcr.io/egordoronchenko/kd-rules-mcp:latest
docker compose up -d --force-recreate
```

Без сохранённого тега — `git checkout <прежний коммит>` и `docker compose up -d --build` (сборка заново). Кэш
структур при откате тоже пересоберётся при следующей загрузке, если код загрузчика отличался.

### Переход с kd2-rules-mcp

1. `git pull` — репозиторий переименован в `kd-rules-mcp`; старый адрес GitHub перенаправляется, но `git remote
   set-url origin https://github.com/egordoronchenko/kd-rules-mcp.git` избавит от перенаправления.
2. `uv sync` — пакет теперь `kd_rules_mcp`.
3. `uv run python scripts/setup_local.py` — перепишет `.env`, `docker-compose.override.yml`, `.mcp.json` и
   `.cursor/mcp.json` под новый ключ сервера `kd-rules-mcp` и имя контейнера `kd_rules_mcp`; старый ключ
   `kd2-rules-mcp` из файлов клиентов убирается. Скрипт печатает команду остановки прежнего контейнера
   `kd2_rules_mcp`, если он ещё запущен.
4. `docker compose up -d --build`; том кэша структур и `workspace\` остаются прежними — проекты правил и
   проекты модулей переживают переименование.
5. В клиентах (Claude Code, Cursor) — перечитать настройки MCP (новый сеанс); в проектах 1С — снова
   `uv run python scripts/build_packs.py --dest <папка проекта> --client claude` (или `agents`): установщик
   удалит папки прежних имён скиллов и `KD2-RULES.md`.

Не меняется: имена инструментов MCP, идентификаторы проверок, коды ошибок, переменные окружения `KD2_*`,
`projects.yaml`.

## Если не получилось

| Где | Симптом | Что делать |
|---|---|---|
| 1 | `docker` не найден / «cannot connect to the Docker daemon» | запустить Docker Desktop, дождаться «Engine running», открыть новое окно PowerShell |
| 2 | `uv sync` не качает пакеты | прокси/сеть; `uv sync -v` покажет, что именно не скачалось |
| 3 | `ProjectConfigError` | текст называет поле и проект; частое — `role` не `песочница`/`боевая`, база ссылается на несуществующую конфигурацию |
| 5 | `setup_local.py`: «Нет projects.yaml» / «Нет папок проектов» | шаги 3–4; путь в `projects.local.yaml` — папка проекта, не выгрузки |
| 5 | на месте `projects.yaml` появилась **папка** | запускали `docker compose up` раньше шага 3: остановить (`docker compose down`), удалить папку, пройти шаги 3–5 |
| 5 | `check_server.py`: сервер недоступен | `docker compose ps`, `docker compose logs kd-rules-mcp --tail 30`; порт 8060 не занят другим? |
| 5 | «папка НЕ видна» | проект не в `projects.local.yaml` или после правки не запускали `setup_local.py` и `docker compose up -d` |
| 6 | агент не видит инструменты | `.mcp.json` записан в текущей сессии — перезапустить клиент; затем Claude Code: `claude mcp list`, разрешить сервер; Cursor: включить сервер в настройках MCP |
| 6 | `structure_load_project`: «Нет каталога выгрузки» | путь `dump`/`extensions` в `projects.yaml` не совпадает с папками проекта (регистр букв не важен, `/` и `\` равноправны) |
| 7 | `prepare`: «База КД версии X, конфигурация Y» | база создана не из свежего шаблона: откройте её один раз в 1С (обновление), затем `prepare` |
| 7 | `check` висит до таймаута | в копии базы всплыло окно: повторить `prepare`; проверить, что `base\1Cv8.1CD` — база КД |
| 8 | «Неверно указан пользователь или пароль» | логин базы — `dev_env` или `logins` (шаг 8.2) |
| 8 | «проверка подключается только к песочницам» | так задумано: у базы `role: боевая`; проверяйте на копии |
| 9 | после обновления агент получает `project_not_found` | снимок проекта не восстановился (в логе контейнера «Снимок … пропущен») — открыть сохранённый файл заново (`rules_open`) |
| 9 | `docker compose up -d --build` падает на сборке | `docker compose logs`/вывод сборки; откат — раздел 9, «Откат» |
| 9 | `check_server.py` после обновления: «папка НЕ видна» | не запускали `setup_local.py` после `git pull` |
