FROM python:3.11-alpine

# Установка зависимостей ОС, необходимых для сборки asyncpg, и утилиты pg_dump
RUN apk update && apk add --no-cache \
    postgresql-client \
    gcc \
    musl-dev \
    postgresql-dev \
    libffi-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# По умолчанию запускаем бота, но docker-compose переопределит команду для API
CMD ["python", "bot.py"]