# Torrent Media Manager (v1.1.0)

Веб-приложение для автоматизации загрузок через qBittorrent, глубокого анализа аудио/субтитров и распределения медиатеки по структуре папок Plex, Jellyfin и Emby.

---

## 🌟 Основные возможности

- **Поддержка Magnet-ссылок и `.torrent` файлов**:
  - Встроенный чистый Python bencode-парсер с автоматическим вычислением SHA-1 `info_hash`.
  - Поддержка кодировок UTF-8 и CP1251 для отечественных трекеров.
  - Устранение задержек и race-conditions при добавлении торрента.
- **Инспекция контейнеров (`mkvmerge -J`)**:
  - Полный анализ внутренних дорожек видео: кодеки (AC3, DTS, TrueHD, FLAC, AAC), раскладка каналов (2.0, 5.1, 7.1), битрейт, язык по стандарту ISO и теги.
- **Mux Studio (Интерактивная склейка)**:
  - Выборочное сохранение и удаление внутренних дорожек видео.
  - Подключение внешних аудиодорожек и субтитров из папки релиза.
  - Настройка порядка дорожек (reordering).
  - Установка флагов по умолчанию (`default`) и форсированных субтитров (`forced`).
  - Компенсация задержки звука (`--sync delay ms`).
  - Неблокирующий фоновый воркер с отображением процента склейки в реальном времени.
- **Real-Time мониторинг (Server-Sent Events)**:
  - Потоковое обновление скоростей, прогресса загрузки и дискового пространства без постоянных опросов (polling).
- **Автоматический импорт и сортировка**:
  - Раскладка по директориям: Фильмы (`Films`), Сериалы (`Serials`), Аниме (`Anime`), Мультфильмы (`Mults`).
  - Форматирование имен по стандартам Plex/Jellyfin (`Show (Year) - S01E01 - Title.mkv`).
- **Контроль дискового пространства (Space Guard)**:
  - Проверка доступного места с учетом qBittorrent preallocation и активных загрузок.
  - Автоматическая очередь `queued_space` при нехватке места.
- **Архитектурная надежность**:
  - База данных SQLite в режиме **WAL** (Write-Ahead Logging).
  - Постоянный журнал истории завершенных импортов (`import_history`).
  - Пул постоянных соединений с qBittorrent API.

---

## 🏗️ Архитектура

```
                ┌───────────────────────────────────┐
                │   Web UI (Modern Glassmorphism)   │
                │   - Drag & Drop .torrent / Magnet │
                │   - TMDB Cards & Metadata         │
                │   - Mux Studio (Track Matrix)     │
                │   - Live Progress & SSE Stream    │
                └─────────────────┬─────────────────┘
                                  │ HTTP / SSE
                                  ▼
                ┌───────────────────────────────────┐
                │        FastAPI Core (v1.1.0)      │
                │   - Bencode Torrent Parser        │
                │   - MediaInspector (mkvmerge -J)  │
                │   - Async Mux Worker + Tracker    │
                │   - Space Manager & Scheduler     │
                └─────┬──────────────┬────────────┬─┘
                      │              │            │
       ┌──────────────┴──┐    ┌──────┴──────┐   ┌─┴────────────────┐
       │   qBittorrent   │    │  mkvmerge   │   │   SQLite (WAL)   │
       │   (Pool Session)│    │ (Async CLI) │   │ (Jobs & History) │
       └─────────────────┘    └─────────────┘   └──────────────────┘
```

---

## 🚀 Развертывание

### 1. Автоматическая раскатка на боевом сервере (RHEL 10, SELinux disabled)

Для быстрой и полностью автоматизированной раскатки на **Red Hat Enterprise Linux 10** (а также Rocky Linux 10 / AlmaLinux 10) подготовлен скрипт `deploy.sh`:

```bash
# 1. Клонирование репозитория
git clone https://github.com/Phoenix1302/torrent-media-manager.git
cd torrent-media-manager

# 2. Выдача прав и запуск авто-деплоя
chmod +x deploy.sh
./deploy.sh
```

**Что делает скрипт:**
- Проверяет ОС RHEL 10 и статус SELinux.
- Проверяет и при необходимости устанавливает `podman` и `curl` через `dnf`.
- Автоматически определяет режим (Rootless с включением `loginctl enable-linger` или Root System Service).
- Создает каталоги хранения (`/opt/torrent-media-manager/data`, `/mnt/dlc_data/...`, `/mnt/main_data/...`) и выставляет права `UID:GID 1000:1000`.
- Создает файл конфигурации из `.env.example` с безопасными правами (`chmod 600`).
- Собирает образ `localhost/torrent-media-manager_torrent-media-manager:v1.1.0`.
- Устанавливает Quadlet unit-файл в `~/.config/containers/systemd/` (или `/etc/containers/systemd/`).
- Перезагружает `systemd`, запускает сервис и проводит проверку работоспособности через `/api/health`.

**Команды управления сервисом:**
```bash
./deploy.sh status    # Проверка статуса
./deploy.sh logs      # Просмотр логов в реальном времени
./deploy.sh restart   # Перезапуск сервиса
./deploy.sh stop      # Остановка сервиса
```

---

### 2. Ручная установка Quadlet (Systemd Service)

Файл конфигурации Quadlet расположен в `systemd/torrent-media-manager.container`.

1. Скопируйте файл в каталог systemd пользователя:
   ```bash
   mkdir -p ~/.config/containers/systemd
   cp systemd/torrent-media-manager.container ~/.config/containers/systemd/
   ```
2. Настройте файл переменных окружения `~/.config/torrent-media-manager/env` на основе `.env.example`:
   ```bash
   mkdir -p ~/.config/torrent-media-manager
   cp .env.example ~/.config/torrent-media-manager/env
   nano ~/.config/torrent-media-manager/env
   ```
3. Перезагрузите systemd и запустите сервис:
   ```bash
   systemctl --user daemon-reload
   systemctl --user restart torrent-media-manager
   systemctl --user status torrent-media-manager
   ```

### 2. Локально: Podman Desktop / Podman Compose

Сборка и запуск контейнера:
```bash
podman build -t localhost/torrent-media-manager:v1.1.0 -f Containerfile .
podman run -d --name torrent-media-manager \
  --network host \
  -v ./data:/app/data:Z \
  localhost/torrent-media-manager:v1.1.0
```

Интерфейс доступен по адресу: `http://localhost:8000/`.

---

## 🧪 Тестирование

Проект содержит 45 автоматизированных тестов, охватывающих bencode-парсер, SQLite WAL, клиент qBittorrent, инспектор медиа и генератор команд mkvmerge.

Запуск тестового набора внутри Podman:
```bash
podman run --rm localhost/torrent-media-manager:v1.1.0 pytest -v /app/tests
```

---

## ⚙️ Переменные конфигурации (.env)

| Переменная | Описание | Значение по умолчанию |
| :--- | :--- | :--- |
| `QBITTORRENT_HOST` | Адрес Web UI qBittorrent | `http://127.0.0.1:8080` |
| `QBITTORRENT_USER` | Логин qBittorrent | `admin` |
| `QBITTORRENT_PASS` | Пароль qBittorrent | `adminadmin` |
| `TMDB_API_KEY` | API-ключ themoviedb.org | — |
| `MOVIES_PATH` | Каталог для фильмов | `/mnt/main_data/Media/Video/Films` |
| `TV_PATH` | Каталог для сериалов | `/mnt/main_data/Media/Video/Serials` |
| `ANIME_PATH` | Каталог для аниме | `/mnt/main_data/Media/Video/Anime` |
| `CARTOONS_PATH` | Каталог для мультфильмов | `/mnt/main_data/Media/Video/Mults` |
| `STAGING_PATH` | Временный каталог для сборки | `/mnt/main_data/Media/InComplete` |
| `DOWNLOAD_DISK_PATH` | Точка монтирования диска загрузок | `/mnt/dlc_data/torrents` |
| `MIN_FREE_DLC_SPACE_GB`| Минимальный порог свободного места | `20` |
| `IMPORT_MODE` | Режим переноса (`copy`, `move`, `hardlink`) | `copy` |
