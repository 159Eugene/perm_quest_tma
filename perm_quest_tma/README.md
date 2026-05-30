🗺 Perm Quest Bot — Enterprise Telegram BotАсинхронный, отказоустойчивый Telegram-бот для проведения интерактивных пеших квестов по историческому центру Перми. Проект спроектирован с учетом высоких нагрузок, содержит встроенный визуальный конструктор квестов, систему античита по GPS и защиту от DDoS.🚀 Технологический стекЯзык: Python 3.11+Фреймворк: aiogram 3.xБаза данных: PostgreSQL 15 + SQLAlchemy 2.0 (asyncpg)Стейт-менеджер и Кэш: Redis 7 (FSM + Throttling)Планировщик: APScheduler (с RedisJobStore)Инфраструктура: Docker, Docker Compose, Multi-stage builds🛠 1. Подготовка VPS к деплоюУбедитесь, что на вашем сервере (Ubuntu/Debian) установлены git, docker и docker-compose.# Обновление пакетов и установка Docker
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose git

# Добавление текущего пользователя в группу docker (чтобы не писать sudo)
sudo usermod -aG docker $USER
newgrp docker
⚙️ 2. Конфигурация окружения (.env)В корневой директории проекта создайте файл .env и заполните его вашими данными. Пример файла уже есть в репозитории, но перед продакшеном обязательно измените пароли и токены.# Настройки Telegram
BOT_TOKEN=123456789:ABCDefghIJKLmnopQRSTuvwxYZ
ADMIN_IDS=[111111111, 222222222] # ID администраторов через запятую

# Настройки PostgreSQL
DB_HOST=quest_postgresql
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=your_super_secure_password_2026
DB_NAME=quest_db

# Настройки Redis
REDIS_HOST=quest_redis
REDIS_PORT=6379
🐳 3. Запуск проекта (Deploy)Проект использует docker-compose.yml для поднятия изолированной сети из трех контейнеров: База данных (Postgres), Кэш (Redis) и само Приложение (Бот).Выполните команду в корне проекта:docker-compose up -d --build
Флаг --build запустит Multi-stage сборку образа бота (установит gcc, скомпилирует asyncpg в wheels и перенесет в легковесный alpine-контейнер).Проверьте статус запущенных контейнеров:docker-compose ps
Все три контейнера (tg_quest_bot, quest_postgresql, quest_redis) должны иметь статус Up.📊 4. Мониторинг и ЛогированиеВ боте настроено продвинутое логирование с ротацией файлов. Все логи пробрасываются как в консоль контейнера, так и в локальные файлы внутри контейнера (и могут быть вынесены в volume при необходимости).Посмотреть логи бота в реальном времени (tail):docker logs -f tg_quest_bot
Посмотреть логи базы данных:docker logs -f quest_postgresql
💾 5. Резервное копирование (Backups)Бот автоматически делает дампы базы данных (используя встроенный pg_dump) каждую ночь в 03:00 по времени Екатеринбурга (Пермь).Дампы сохраняются в Docker Volume backups_data. Чтобы достать бэкап на хост-машину, выполните:# Скопировать папку /backups из контейнера БД на ваш сервер в папку ./local_backups
docker cp quest_postgresql:/backups ./local_backups
🎮 6. Руководство АдминистратораПосле запуска бота, отправьте ему команду /start с аккаунта, чей ID указан в ADMIN_IDS.Доступные команды администратора:/admin — Открыть визуальный конструктор квестов (создание узлов, веток, привязка фото/аудио, гео-координат)./broadcast [текст] — Запустить массовую рассылку всем зарегистрированным пользователям (с задержкой для защиты от лимитов Telegram)./unban [Telegram ID] — Вручную разблокировать пользователя, если он случайно попал под античит (Shadow Ban)./reset_session [Telegram ID] — Принудительно сбросить зависшую активную сессию игрока (возврат в главное меню).