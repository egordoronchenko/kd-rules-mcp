# Шаг 7 (необязательно): база КД для kd_check

## 7. Необязательно: база КД для `kd_check`

Шаги 7–8 готовят ступени `kd_check` и `bsp_check` лестницы проверок; что они доказывают и как читать итог —
`docs/checks.md` (здесь — только установка). Только если человек хочет финальную проверку штатной загрузкой КД. Нужны платформа 1С 8.3 и `1Cv8.cf`
«Конвертации данных» 2.1 (поставка с ИТС) — путь спросить.

```powershell
& "<путь к 1cv8.exe>" CREATEINFOBASE File="<папка репозитория>\base" /UseTemplate "<путь к 1Cv8.cf>"
uv run python kdbase/kd_check.py prepare
uv run python kdbase/kd_check.py check tests/data/exchange_rules.xml
```

Проверка: `prepare` → «пользователь «Агент» готов»; `check` → `ИТОГ OK` и `КОНЕЦ`. База `base\` — только для
проверок, в git не коммитить.
