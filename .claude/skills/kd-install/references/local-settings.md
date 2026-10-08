# Шаг 4: projects.local.yaml

## 4. `projects.local.yaml`

```powershell
copy projects.local.example.yaml projects.local.yaml
```

В `projects:` — папки проектов на этой машине (ключи — как в `projects.yaml`). Остальное оставить, кроме двух
случаев (проверить до шага 5: `docker ps` — занят ли порт 8060 и есть ли уже контейнер `kd_rules_mcp`):

- **порт 8060 занят** — `port: 8061` (или другой свободный) и `server_url` с тем же портом
  (`http://localhost:8061/mcp`);
- **второй экземпляр сервера на этой машине** — `instance: <суффикс>` (буквы, цифры, `_`, `-`) и свой `port`:
  контейнер, том кэша и проект compose получают суффикс (`kd_rules_mcp_<суффикс>`), первый экземпляр не
  затрагивается.

`setup_local.py` (шаг 5) пишет из этих полей `.env` клона (`KD2_PUBLISHED_PORT`, `KD2_BIND`, `KD2_CONTAINER`,
`KD2_CACHE_VOLUME`, `COMPOSE_PROJECT_NAME`); тот же код `setup` в образе работает без uv на хосте.
`image_tag: X.Y.Z` закрепляет образ релиза без префикса `v` (`KD_IMAGE_TAG` в `.env`);
без поля используется `latest`. `.env` и `docker-compose.yml` руками не править.
