# Плагины Claude Code — Method Maximum

Внутренний маркетплейс агентства. Пока в нём один плагин.

| Плагин | Что делает |
|---|---|
| `recon-site` | Разведка структуры сайта и ниши до КП (Этап 02 регламента): спрос в Wordstat, выдача Яндекса и Google, структуры конкурентов, готовая структура сайта для листа «0. Структура сайта» калькулятора |

## Установка

Нужен Claude Code (CLI или десктоп) и доступ к этому репозиторию.

В Claude Code:

```
/plugin marketplace add metodmaximum-crypto/claude-plugins
/plugin install recon-site@methodmaximum
```

Перезапустите Claude Code. Скилл подхватывается сам, когда в задаче есть
разведка сайта или ниши; явно — `/recon-site:recon-site`.

## Ключи

Ключи **не хранятся в репозитории и не лежат в папке плагина** — она
заменяется при каждом обновлении. Место для них — `~/.recon-site/.env`
(Windows: `%USERPROFILE%\.recon-site\.env`).

1. Создайте папку `.recon-site` в домашней папке.
2. Скопируйте туда `.env.example` из плагина под именем `.env`.
3. Заполните:
   - `YC_API_KEY`, `YC_FOLDER_ID` — Yandex Cloud, Search API (Wordstat и выдача).
     Роль сервисного аккаунта — `search-api.executor`. `folderId` начинается
     с `b1g`; ID с `aje` — это сервисный аккаунт, не каталог.
   - остальное — по необходимости, см. комментарии в файле.

Проверка: попросите Claude «проверь доступ к Яндексу для recon-site» или
запустите `python scripts/check_access.py "фраза" 225` из папки скилла.

Без ключей работают аудит сайта и разбор структур конкурентов.

## Обновление

```
/plugin marketplace update methodmaximum
/plugin update recon-site@methodmaximum
```

После обновления — перезапуск Claude Code. Ключи в `~/.recon-site/.env`
при этом не трогаются.

## Что нужно знать

- **Только Claude Code.** Скрипты ходят в API Яндекса и на сайты конкурентов,
  в чате claude.ai сеть ограничена. Режим браузера требует расширения
  Claude in Chrome.
- **Лимит Wordstat — 100 обращений в час на ключ.** На одном ключе лимит у
  всех общий. Скрипт считает бюджет перед стартом и останавливается на первом
  отказе, не сжигая квоту повторами. Поднять лимит — только тикетом в
  поддержку Yandex Cloud.
- **Google-выдачу определяет IP.** Запросы без названия места в тексте
  снимаются только из нужной страны. `serp.py` пишет страну выхода в артефакт.
- Зависимостей нет: только стандартная библиотека Python 3.

## Разработка

```
plugins/recon-site/
├── .claude-plugin/plugin.json
└── skills/recon-site/
    ├── SKILL.md
    ├── .env.example
    ├── references/     классификация выдачи, У/Т/С, критерий готовности
    └── scripts/        wordstat, serp, site_audit, competitor, check_access
```

Меняете поведение — поднимите `version` в `plugin.json`, иначе у коллег
обновление может не подтянуться. Перед коммитом:

```
claude plugin validate .
claude plugin validate plugins/recon-site
```

Секреты в репозиторий не коммитить: `.env` в `.gitignore`.
