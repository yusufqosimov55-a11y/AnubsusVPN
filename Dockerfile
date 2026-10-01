FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости, если нужны
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код
COPY . .

# Команда запуска вашего бота (если файл называется main.py)
CMD ["python", "main.py"]
