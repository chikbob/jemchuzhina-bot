# Развёртывание на PythonAnywhere

Демонстрационный бот: [MariupolPearlBookingBot](https://t.me/MariupolPearlBookingBot). Веб-приложение: `https://chikbob.pythonanywhere.com`, проект на сервере: `/home/chikbob/jemchuzhena_bot`.

## Первое размещение

1. Загрузите исходный код в домашний каталог аккаунта, вне публичного каталога static. Создайте там приватный `.env` по `.env.example`; задайте `BOT_TOKEN`, `ADMIN_PASSWORD`, `WEBHOOK_SECRET` и `WEBHOOK_BASE_URL=https://chikbob.pythonanywhere.com`.
2. В Bash-консоли выполните:

```bash
cd ~/jemchuzhena_bot
bash deploy/pythonanywhere_setup.sh
```

3. В разделе Web создайте приложение: Manual configuration, Python 3.12. Source code и Working directory: `/home/chikbob/jemchuzhena_bot`. Virtualenv: `/home/chikbob/jemchuzhena_bot/.venv`.
4. Замените содержимое WSGI configuration file кодом из [pythonanywhere_wsgi.py](pythonanywhere_wsgi.py), затем нажмите Reload.
5. Проверьте `/health`: ожидается HTTP 200 и `status: ok`.
6. Настройте webhook:

```bash
.venv/bin/python scripts/configure_webhook.py
```

7. Откройте бота и нажмите `/start`. Владелец входит через `/admin` с паролем; назначенные менеджеры и администраторы входят без пароля.

## Обновление действующего приложения

Архив обновления должен содержать только исходный код, тесты и документацию. Сохраните серверные `.env` и `data/pearl.sqlite3`: они не должны попадать в Git или заменяться демонстрационными файлами.

Сначала создайте согласованную резервную копию SQLite:

```bash
.venv/bin/python - <<'PY'
import sqlite3
from contextlib import closing
import time
from pathlib import Path

backup_dir = Path('data/backups')
backup_dir.mkdir(exist_ok=True)
backup_dir.chmod(0o700)
backup = backup_dir / f'before-update-{int(time.time())}.sqlite3'
with closing(sqlite3.connect('data/pearl.sqlite3')) as source:
    with closing(sqlite3.connect(backup)) as target:
        source.backup(target)
backup.chmod(0o600)
print('Backup created')
PY
```

Затем распакуйте обновление, выполните тесты и нажмите Reload. Новые поля комментариев и архива добавляются к существующей базе автоматически; история записей остаётся на месте. После обновления используйте `/start`, чтобы открыть новый интерфейс.

## Как работает бесплатный режим

Flask принимает webhook Telegram и вызывает сценарии aiogram. Секретный заголовок проверяется до обработки запроса. Запросы сериализуются файловой блокировкой; update ID, анкеты и очереди сохраняются в SQLite. Повтор доставки не повторяет завершённый шаг и не создаёт вторую запись.

WSGI worker переиспользует соединения с Telegram. Подтверждение нажатия передаётся в ответе webhook, а устаревшие неотправленные изменения календаря заменяются новыми. При выборе даты не отправляются служебные уведомления и публикации, которые могли бы задержать кнопки.

Уведомления, повторы после сетевой ошибки и обновление поста отправляются при входящих событиях. Фонового расписания на бесплатном плане нет. Прошедшие окна фильтруются и архивируются при обращении к боту; записи по ним не удаляются.

[Официальное описание бесплатных аккаунтов](https://help.pythonanywhere.com/pages/FreeAccountsFeatures/) и [список разрешённых внешних адресов](https://www.pythonanywhere.com/whitelist/). Проверяйте срок приложения в разделе Web и вовремя нажимайте «Run until 1 month from today».

Не запускайте polling одновременно с webhook. Для polling сначала отключите webhook или используйте отдельного тестового бота. Не выводите токен, пароль или секрет webhook в консоль и журналы.
