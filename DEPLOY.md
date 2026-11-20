# Развёртывание на хостинге (панель с `main.py`)

Панели вроде Pterodactyl / Pelican запускают бота как `python main.py` и ставят
зависимости через `pip`. Проект использует `uv` для разработки, но для панели есть
адаптер — `main.py` в корне репозитория.

## Что отвечать в панели

| Поле | Ответ |
|---|---|
| **Bot Main File** | `main.py` |
| **Среда выполнения** | `Python 3.13` |
| **User Uploaded Files** | включено, **если** заливаешь файлы сам через SFTP |
| **Git Repository Address** | URL репозитория, если будешь деплоить через git (иначе пусто) |
| **Git Branch** | `master` (или твоя ветка) |
| **Additional Python Packages** | `-r requirements.txt` |

Если заливаешь через git — `User Uploaded Files` можно оставить включённым, он не
мешает.

## Обязательные файлы на сервере

Без них бот не запустится:

```
main.py
requirements.txt
pyproject.toml
src/dsbot/…            весь код
forms/training.yaml    форма рапорта
forms/ladders/         лестницы рангов (когда настроишь)
.env                   токен (создать вручную, в git его нет)
```

`var/` создастся сам — там SQLite.

## Создать `.env` на сервере

Файл в `.gitignore`, поэтому его никогда не будет в репозитории. Содержимое:

```dotenv
DSBOT_DISCORD_TOKEN=твой_токен
DSBOT_DATABASE_URL=sqlite+aiosqlite:///./var/dsbot.db
DSBOT_LOG_LEVEL=INFO
DSBOT_LOG_FORMAT=console
DSBOT_DEFAULT_TIMEZONE=Europe/Moscow
DSBOT_DEFAULT_LOCALE=ru
DSBOT_CONTACT_USER_ID=263344369451401227
DSBOT_REPORT_GROUP_WINDOW_SECONDS=120
DSBOT_ENABLE_MEMBERS_INTENT=false
```

Путь к БД **относительный** (`./var`), поэтому рабочий каталог процесса должен быть
корнем проекта. Панель так и делает.

## Первый запуск

Панель сама выполнит `pip install -r requirements.txt` и запустит `main.py`.
Таблицы создаются автоматически при старте — `init-db` не нужен.

Если панель даёт консоль, проверь:

```bash
python main.py doctor
```

Покажет, видит ли бот токен, серверы и каналы.

## Привязка каналов на сервере

Однократно, через консоль панели (не через `run`):

```bash
python main.py form-apply --channel <ID> --file forms/training.yaml --guild <ID гильдии>
python main.py channels
```

`form-apply` сам создаёт привязку, если её нет. Повторный вызов безопасен.

## Перезапуск и остановка

Панель обычно сама убивает процесс по кнопке. Внутри консоли:

```bash
python main.py status
python main.py stop
```

`stop` работает и на Linux: он читает pid-файл и дополнительно ищет процессы
`dsbot run` через `ps`, потому что `uv run` может оставить ребёнка живым.

## Обновление кода

- **через git:** панель перетянет ветку при переустановке/рестарте
- **через файлы:** замени `src/` и перезапусти

Зависимости меняются редко; если менялись — переустанови (`pip install -r requirements.txt`).

## Хранение данных

`var/dsbot.db` — вся история. Панель может считать это временными файлами при
переустановке сервера, поэтому:

1. Скачай `var/dsbot.db` перед любым «Reinstall».
2. Для бэкапов достаточно скопировать этот файл (SQLite в режиме WAL, поэтому бери
   вместе с `-wal`, либо останавливай бота перед копированием).

## Если панель не принимает `-r requirements.txt`

Впиши пакеты напрямую, через пробел:

```
discord-py aiosqlite sqlalchemy pydantic pydantic-settings structlog typer pyyaml tzdata
```

Версии есть в `requirements.txt`, но обычно достаточно свежих.

## Отличия Linux от Windows

| | Windows | Linux (панель) |
|---|---|---|
| Остановка процесса | PowerShell + CIM | `ps` |
| Пути | `\` | `/` (в коде всё через `pathlib`) |
| Кодировка консоли | cp866, чинится принудительным UTF-8 | UTF-8 из коробки |

Файловая блокировка (`var/dsbot.lock`) реализована и через `msvcrt`, и через `fcntl`.
