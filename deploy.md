# Руководство по развёртыванию «Свадебный Раш»

Пошаговая инструкция: локальный запуск в Docker и деплой на Railway.

---

## Содержание

1. [Предварительные требования](#предварительные-требования)
2. [Локальный запуск в Docker](#локальный-запуск-в-docker)
3. [Деплой на Railway](#деплой-на-railway)
4. [Проверка работоспособности](#проверка-работоспособности)
5. [Troubleshooting](#troubleshooting)

---

## Предварительные требования

| Инструмент | Минимальная версия | Установка |
|---|---|---|
| Docker | 24.0+ | [docker.com](https://docs.docker.com/get-docker/) |
| Docker Compose | 2.20+ | Входит в Docker Desktop |
| Git | 2.30+ | [git-scm.com](https://git-scm.com/downloads) |
| Railway CLI | 3.0+ | [railway.app](https://docs.railway.com/develop/cli) |

---

## Локальный запуск в Docker

### Шаг 1 — Клонировать проект

```bash
git clone <url-репозитория>
cd wedding
```

Или, если проект уже локально:

```bash
cd /path/to/wedding
```

### Шаг 2 — Собрать и запустить через Docker Compose

```bash
docker compose up --build
```

Флаг `--build` гарантирует, что образ пересоберётся при изменениях в коде.

Для запуска в фоновом режиме:

```bash
docker compose up --build -d
```

### Шаг 3 — Проверить работу

Откройте в браузере:

```
http://localhost:8000
```

### Шаг 4 — Остановить

```bash
docker compose down
```

### Альтернатива: запуск без Compose

Если Docker Compose не установлен:

```bash
# Собрать образ
docker build -t wedding-rash .

# Запустить контейнер
docker run -p 8000:8000 --name wedding-rash wedding-rash
```

Остановить:

```bash
docker stop wedding-rash
docker rm wedding-rash
```

---

## Деплой на Railway

[Railway](https://railway.app) — платформа для деплоя с автоматическим CI/CD из GitHub-репозитория. Бесплатный тариф включает $5 кредитов в месяц.

### Шаг 1 — Подготовить репозиторий

Убедитесь, что проект в Git-репозитории и запушен на GitHub:

```bash
git init
git add .
git commit -m "Initial commit: Wedding Rush game"
git remote add origin https://github.com/<ваш-username>/wedding-rash.git
git push -u origin main
```

### Шаг 2 — Установить Railway CLI

**macOS (Homebrew):**

```bash
brew install railway
```

**Linux / macOS (curl):**

```bash
bash <(curl -fsSL https://railway.com/install.sh)
```

**Через npm:**

```bash
npm install -g @railway/cli
```

### Шаг 3 — Авторизоваться в Railway

```bash
railway login
```

Откроется браузер для авторизации через GitHub или email.

### Шаг 4 — Инициализировать проект на Railway

```bash
railway init
```

При запросе:
- **Project name** — введите имя, например `wedding-rash`
- **Environment** — выберите `production`

### Шаг 5 — Подключить репозиторий

```bash
railway service create wedding-rash
railway service link wedding-rash
```

### Шаг 6 — Задать переменные окружения

Railway автоматически определяет Dockerfile. Дополнительные переменные не требуются, но можно задать порт явно:

```bash
railway variables set PORT=8000
```

### Шаг 7 — Задеплоить

```bash
railway up
```

Эта команда:
1. Загрузит исходники на Railway
2. Соберёт Docker-образ
3. Запустит контейнер

### Шаг 8 — Получить публичный URL

```bash
railway domain
```

Или откройте панель управления: [railway.app](https://railway.app) → ваш проект → **Settings** → **Networking** → **Generate Domain**.

Railway выдаст URL вида:

```
https://wedding-rash-production.up.railway.app
```

### Шаг 9 — Открыть игру

Перейдите по полученному URL на смартфоне или ПК. Игра готова к использованию.

---

## Обновление после изменений в коде

### Локально (Docker)

```bash
docker compose up --build
```

### Railway

```bash
git add .
git commit -m "Update game logic"
git push
```

Railway автоматически пересоберёт и перезапустит приложение при каждом пуше в основную ветку.

Для ручного деплоя:

```bash
railway up
```

---

## Проверка работоспособности

После деплоя проверьте:

1. **Главная страница** — `https://<ваш-домен>/` возвращает экран лобби
2. **WebSocket** — при подключении к комнате игра стартует и таймер отсчитывается
3. **Лидерборд** — при подключении нескольких игроков (откройте в 2+ вкладках) рейтинг обновляется в реальном времени
4. **Мобильная адаптация** — откройте URL на смартфоне, проверьте touch-управление корзиной

### Быстрый тест через curl

```bash
# Проверить, что сервер отвечает
curl -I https://<ваш-домен>/

# Ожидаемый ответ: HTTP/2 200
```

---

## Troubleshooting

### Docker: порт 8000 уже занят

```bash
# Найти процесс на порту 8000
lsof -i :8000

# Изменить порт в docker-compose.yml на другой
ports:
  - "3000:8000"
```

### Railway: сборка не запускается

Убедитесь, что в корне репозитория есть `Dockerfile` и `requirements.txt`:

```bash
git ls-files | grep -E "Dockerfile|requirements.txt"
```

Если файлы не отслеживаются:

```bash
git add Dockerfile requirements.txt
git commit -m "Add Docker config"
git push
```

### Railway: WebSocket не подключается

Railway поддерживает WebSocket «из коробки». Если соединение обрывается:

1. Проверьте, что URL использует `wss://` (а не `ws://`) — Railway использует HTTPS
2. Код клиента в `game.js` уже определяет протокол автоматически:
   ```javascript
   const wsProtocol = window.location.protocol === "https:" ? "wss://" : "ws://";
   ```

### Docker: образ не пересобирается

```bash
# Принудительная пересборка без кэша
docker compose build --no-cache
docker compose up
```

---

## Структура файлов для деплоя

```
wedding/
├── Dockerfile           # Инструкции сборки образа
├── .dockerignore        # Исключения из контекста Docker
├── docker-compose.yml   # Локальный запуск через Compose
├── main.py              # FastAPI приложение
├── requirements.txt     # Python зависимости
└── static/
    ├── index.html       # Frontend
    └── game.js          # Игровой движок
```
