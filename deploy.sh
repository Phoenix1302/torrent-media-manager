#!/usr/bin/env bash
# ==============================================================================
# Torrent Media Manager v1.1.1 — Automated Deployment Script
# Target OS: Red Hat Enterprise Linux 10 (RHEL 10) / AlmaLinux 10 / Rocky Linux 10
# Container Engine: Podman Quadlet (Systemd Service, Network=Host)
# SELinux: Disabled
# ==============================================================================

set -euo pipefail

APP_NAME="torrent-media-manager"
APP_VERSION="1.1.1"
IMAGE_TAG="localhost/torrent-media-manager_torrent-media-manager:v1.1.1"
IMAGE_ALIAS="localhost/torrent-media-manager:v1.1.1"
OPT_DIR="/opt/torrent-media-manager"
ORIG_DIR=""

# ANSI Colors
C_RESET="\033[0m"
C_RED="\033[1;31m"
C_GREEN="\033[1;32m"
C_YELLOW="\033[1;33m"
C_BLUE="\033[1;34m"
C_CYAN="\033[1;36m"
C_BOLD="\033[1m"

log_info()    { echo -e "${C_CYAN}[INFO]${C_RESET} $*"; }
log_success() { echo -e "${C_GREEN}[OK]${C_RESET} $*"; }
log_warn()    { echo -e "${C_YELLOW}[WARN]${C_RESET} $*"; }
log_error()   { echo -e "${C_RED}[ERROR]${C_RESET} $*" >&2; }

# Locate script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

print_banner() {
  echo -e "${C_BLUE}══════════════════════════════════════════════════════════════════${C_RESET}"
  echo -e "${C_BOLD}  Torrent Media Manager v${APP_VERSION} — Deployment Wizard${C_RESET}"
  echo -e "  Red Hat Enterprise Linux 10 • Podman Quadlet • Network=Host"
  echo -e "${C_BLUE}══════════════════════════════════════════════════════════════════${C_RESET}"
}

# ------------------------------------------------------------------------------
# 1. Environment & Pre-Flight Checks
# ------------------------------------------------------------------------------
check_system() {
  log_info "1/7 Проверка операционной системы и окружения..."

  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    log_info "Обнаружена ОС: ${PRETTY_NAME:-$NAME $VERSION_ID}"
  else
    log_warn "Не удалось точно определить ОС через /etc/os-release, продолжаем."
  fi

  # Check SELinux status
  if command -v getenforce >/dev/null 2>&1; then
    SELINUX_STATUS=$(getenforce)
    log_info "Статус SELinux: ${SELINUX_STATUS}"
    if [[ "${SELINUX_STATUS}" == "Enforcing" ]]; then
      log_warn "Внимание: SELinux находится в режиме Enforcing!"
      log_warn "Для корректной работы хост-монтирований рекомендуется отключить SELinux:"
      log_warn "  sudo setenforce 0"
      log_warn "  sudo sed -i 's/^SELINUX=enforcing/SELINUX=disabled/' /etc/selinux/config"
    fi
  else
    log_info "Утилита getenforce не найдена (SELinux отключен)."
  fi

  # Check Podman
  if ! command -v podman >/dev/null 2>&1; then
    log_info "Podman не найден. Установка podman через dnf..."
    if [[ "$(id -u)" -eq 0 ]]; then
      dnf install -y podman curl
    else
      sudo dnf install -y podman curl
    fi
  fi
  PODMAN_VER=$(podman --version)
  log_success "Контейнерный движок: ${PODMAN_VER}"

  # Configure safe directory for git to prevent dubious ownership warnings
  if command -v git >/dev/null 2>&1; then
    git config --global --add safe.directory "${OPT_DIR}" 2>/dev/null || true
  fi
}

# ------------------------------------------------------------------------------
# 2. Canonical Location Check (/opt/torrent-media-manager)
# ------------------------------------------------------------------------------
ensure_canonical_location() {
  ORIG_DIR="${SCRIPT_DIR}"
  if [[ "${SCRIPT_DIR}" != "${OPT_DIR}" ]]; then
    log_warn "Скрипт запущен из '${ORIG_DIR}', а не из канонической папки '${OPT_DIR}'."
    log_info "Автоматическое копирование файлов проекта в ${OPT_DIR}..."

    if [[ "$(id -u)" -eq 0 ]]; then
      mkdir -p "${OPT_DIR}"
      if [[ -f "${OPT_DIR}/.env" ]]; then
        cp -an "${ORIG_DIR}/." "${OPT_DIR}/" 2>/dev/null || cp -a "${ORIG_DIR}/." "${OPT_DIR}/"
      else
        cp -a "${ORIG_DIR}/." "${OPT_DIR}/"
      fi
      chown -R "${TARGET_UID}:${TARGET_GID}" "${OPT_DIR}"
      chmod -R 775 "${OPT_DIR}"
    else
      sudo mkdir -p "${OPT_DIR}"
      if [[ -f "${OPT_DIR}/.env" ]]; then
        sudo cp -an "${ORIG_DIR}/." "${OPT_DIR}/" 2>/dev/null || sudo cp -a "${ORIG_DIR}/." "${OPT_DIR}/"
      else
        sudo cp -a "${ORIG_DIR}/." "${OPT_DIR}/"
      fi
      sudo chown -R "${TARGET_UID}:${TARGET_GID}" "${OPT_DIR}"
      sudo chmod -R 775 "${OPT_DIR}"
    fi

    log_success "Код проекта успешно размещен в ${OPT_DIR}."
    log_info "Переключение рабочей директории в ${OPT_DIR}..."
    cd "${OPT_DIR}"
    SCRIPT_DIR="${OPT_DIR}"
  else
    log_info "Каноническое размещение подтверждено: ${OPT_DIR}"
  fi
}

# ------------------------------------------------------------------------------
# 3. Determine Deployment Mode (Rootless vs Root/System)
# ------------------------------------------------------------------------------
detect_mode() {
  if [[ "$(id -u)" -eq 0 ]]; then
    DEPLOY_MODE="system"
    QUADLET_DIR="/etc/containers/systemd"
    CONFIG_DIR="${OPT_DIR}"
    SYSTEMCTL_CMD="systemctl"
    log_info "Режим развертывания: SYSTEM (Root Service)"
  else
    DEPLOY_MODE="user"
    QUADLET_DIR="${HOME}/.config/containers/systemd"
    CONFIG_DIR="${OPT_DIR}"
    SYSTEMCTL_CMD="systemctl --user"
    log_info "Режим развертывания: ROOTLESS (User: $(whoami))"

    # Enable systemd user lingering so rootless containers start at boot
    if command -v loginctl >/dev/null 2>&1; then
      log_info "Включение systemd linger для пользователя $(whoami)..."
      loginctl enable-linger "$(whoami)" || true
    fi
  fi
}

# Load custom UID/GID values from env if present
load_user_ids() {
  if [[ -f "${ENV_FILE:-}" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
    set +a
  elif [[ -f "${SCRIPT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "${SCRIPT_DIR}/.env"
    set +a
  fi

  # smbwriter defaults
  TARGET_UID="${PUID:-${TARGET_UID:-1000}}"
  TARGET_GID="${PGID:-${TARGET_GID:-1000}}"
  
  # qbittorrent-nox defaults
  QBIT_UID="${QBIT_UID:-1002}"
  QBIT_GID="${QBIT_GID:-1003}"
  SUPPLEMENTARY_GIDS="${SUPPLEMENTARY_GIDS:-1003}"

  log_info "Пользователи и группы: App UID/GID=${TARGET_UID}:${TARGET_GID} (smbwriter), QBit UID/GID=${QBIT_UID}:${QBIT_GID}, Supplementary GID=${SUPPLEMENTARY_GIDS}"
}

# ------------------------------------------------------------------------------
# 3. Create Storage Directories and Set Permissions
# ------------------------------------------------------------------------------
setup_directories() {
  load_user_ids
  log_info "2/7 Подготовка каталогов хранения и прав доступа..."

  # Application DB / persistent data path
  DATA_DIR="/opt/torrent-media-manager/data"
  if [[ "$(id -u)" -eq 0 ]]; then
    mkdir -p "${DATA_DIR}"
    chown -R "${TARGET_UID}:${TARGET_GID}" "/opt/torrent-media-manager"
    chmod -R 775 "${DATA_DIR}"
  else
    if [[ -w "/opt" || -d "${DATA_DIR}" && -w "${DATA_DIR}" ]]; then
      mkdir -p "${DATA_DIR}"
      chmod 775 "${DATA_DIR}" || true
    else
      log_info "Создание ${DATA_DIR} через sudo..."
      sudo mkdir -p "${DATA_DIR}"
      sudo chown -R "${TARGET_UID}:${TARGET_GID}" "/opt/torrent-media-manager"
      sudo chmod -R 775 "${DATA_DIR}"
    fi
  fi

  # Media destination directories (smbwriter: 1000:1000, 0775)
  MEDIA_DIRS=(
    "/mnt/main_data/Media/InComplete"
    "/mnt/main_data/Media/Video/Films"
    "/mnt/main_data/Media/Video/Serials"
    "/mnt/main_data/Media/Video/Anime"
    "/mnt/main_data/Media/Video/Mults"
  )

  for dir in "${MEDIA_DIRS[@]}"; do
    if [[ ! -d "${dir}" ]]; then
      log_info "Создание каталога медиатеки: ${dir}"
      if [[ "$(id -u)" -eq 0 ]]; then
        mkdir -p "${dir}"
        chown -R "${TARGET_UID}:${TARGET_GID}" "${dir}" || true
        chmod 775 "${dir}" || true
      else
        sudo mkdir -p "${dir}" 2>/dev/null || mkdir -p "${dir}" 2>/dev/null || true
        sudo chown -R "${TARGET_UID}:${TARGET_GID}" "${dir}" 2>/dev/null || true
        sudo chmod 775 "${dir}" 2>/dev/null || true
      fi
    else
      # Ensure group write permission on existing directory
      if [[ "$(id -u)" -eq 0 ]]; then
        chmod 775 "${dir}" || true
      else
        sudo chmod 775 "${dir}" 2>/dev/null || chmod 775 "${dir}" 2>/dev/null || true
      fi
    fi
  done

  # Download directories (qbittorrent-nox: 1002:1003)
  TORRENT_DIRS=(
    "/mnt/dlc_data/torrents"
    "/mnt/dlc_data/torrents/InComplete"
    "/mnt/dlc_data/torrents/Complete"
    "/mnt/main_data/Media/Downloads"
  )

  for dir in "${TORRENT_DIRS[@]}"; do
    if [[ -d "${dir}" ]]; then
      log_info "Корректировка прав на каталог торрентов (g+rwx для группы ${QBIT_GID}): ${dir}"
      if [[ "$(id -u)" -eq 0 ]]; then
        chmod g+rwx "${dir}" || true
      else
        sudo chmod g+rwx "${dir}" 2>/dev/null || chmod g+rwx "${dir}" 2>/dev/null || true
      fi
    else
      log_info "Создание каталога торрентов: ${dir}"
      if [[ "$(id -u)" -eq 0 ]]; then
        mkdir -p "${dir}"
        chown -R "${QBIT_UID}:${QBIT_GID}" "${dir}" || true
        chmod 775 "${dir}" || true
      else
        sudo mkdir -p "${dir}" 2>/dev/null || mkdir -p "${dir}" 2>/dev/null || true
        sudo chown -R "${QBIT_UID}:${QBIT_GID}" "${dir}" 2>/dev/null || true
        sudo chmod 775 "${dir}" 2>/dev/null || true
      fi
    fi
  done

  log_success "Каталоги и права доступа настроены (0775 / g+rwx)."
}

# ------------------------------------------------------------------------------
# 4. Environment File (.env) Configuration
# ------------------------------------------------------------------------------
setup_config() {
  log_info "3/7 Настройка файла переменных окружения (.env)..."

  mkdir -p "${OPT_DIR}"
  ENV_FILE="${OPT_DIR}/.env"

  if [[ ! -f "${ENV_FILE}" ]]; then
    if [[ -n "${ORIG_DIR:-}" && -f "${ORIG_DIR}/.env" && "${ORIG_DIR}/.env" != "${ENV_FILE}" ]]; then
      log_info "Перенос существующего .env из ${ORIG_DIR} в ${ENV_FILE}..."
      cp "${ORIG_DIR}/.env" "${ENV_FILE}"
    elif [[ -f "${SCRIPT_DIR}/.env" && "${SCRIPT_DIR}/.env" != "${ENV_FILE}" ]]; then
      log_info "Копирование существующего .env в ${ENV_FILE}..."
      cp "${SCRIPT_DIR}/.env" "${ENV_FILE}"
    else
      log_info "Создание ${ENV_FILE} на основе .env.example..."
      cp "${SCRIPT_DIR}/.env.example" "${ENV_FILE}"
    fi
    chmod 660 "${ENV_FILE}" 2>/dev/null || chmod 600 "${ENV_FILE}" 2>/dev/null || true
    if [[ "$(id -u)" -eq 0 ]]; then
      chown "${TARGET_UID}:${TARGET_GID}" "${ENV_FILE}" 2>/dev/null || true
    else
      sudo chown "${TARGET_UID}:${TARGET_GID}" "${ENV_FILE}" 2>/dev/null || true
    fi
    log_warn "Создан новый файл конфигурации: ${ENV_FILE}"
    log_warn "Пожалуйста, проверьте в нем QBITTORRENT_PASS и TMDB_API_KEY при необходимости."
  else
    log_success "Файл конфигурации уже существует: ${ENV_FILE}"
  fi
}

# ------------------------------------------------------------------------------
# 5. Build Container Image via Podman
# ------------------------------------------------------------------------------
build_image() {
  log_info "4/7 Сборка контейнерного образа ${IMAGE_TAG}..."

  if [[ ! -f "Containerfile" ]]; then
    log_error "Containerfile не найден в ${SCRIPT_DIR}!"
    exit 1
  fi

  podman build \
    --format docker \
    -t "${IMAGE_TAG}" \
    -t "${IMAGE_ALIAS}" \
    -f Containerfile .

  log_success "Образ собран и протегирован: ${IMAGE_TAG}"
}

# ------------------------------------------------------------------------------
# 6. Install Podman Quadlet Unit
# ------------------------------------------------------------------------------
install_quadlet() {
  log_info "5/7 Установка Systemd Quadlet unit-файла..."

  mkdir -p "${QUADLET_DIR}"
  QUADLET_TARGET="${QUADLET_DIR}/${APP_NAME}.container"

  # Select base unit file
  SRC_UNIT="systemd/${APP_NAME}.container"
  if [[ ! -f "${SRC_UNIT}" ]]; then
    SRC_UNIT="${APP_NAME}.container"
  fi

  if [[ ! -f "${SRC_UNIT}" ]]; then
    log_error "Unit-файл ${SRC_UNIT} не найден!"
    exit 1
  fi

  # Generate target container unit with exact environment file path and user IDs
  sed -e "s|^EnvironmentFile=.*|EnvironmentFile=${ENV_FILE}|g" \
      -e "s|^User=.*|User=${TARGET_UID}:${TARGET_GID}|g" \
      -e "s|^GroupAdd=.*|GroupAdd=${SUPPLEMENTARY_GIDS}|g" \
      "${SRC_UNIT}" > "${QUADLET_TARGET}"
  chmod 644 "${QUADLET_TARGET}"

  log_success "Quadlet установлен в: ${QUADLET_TARGET}"
}

# ------------------------------------------------------------------------------
# 7. Reload and Start Systemd Service
# ------------------------------------------------------------------------------
start_service() {
  log_info "6/7 Перезагрузка systemd daemon и запуск сервиса..."

  ${SYSTEMCTL_CMD} daemon-reload
  ${SYSTEMCTL_CMD} restart "${APP_NAME}"

  log_info "Ожидание запуска сервиса..."
  sleep 3

  if ${SYSTEMCTL_CMD} is-active --quiet "${APP_NAME}"; then
    log_success "Сервис ${APP_NAME} успешно запущен!"
  else
    log_error "Сервис не смог запуститься. Проверка логов:"
    ${SYSTEMCTL_CMD} status "${APP_NAME}" --no-pager || true
    echo ""
    log_error "Последние записи журнала:"
    journalctl ${DEPLOY_MODE:+--user} -u "${APP_NAME}" -n 25 --no-pager || true
    exit 1
  fi
}

# ------------------------------------------------------------------------------
# 8. Health Check Verification
# ------------------------------------------------------------------------------
verify_health() {
  log_info "7/7 Проверка работоспособности API (/api/health)..."

  MAX_ATTEMPTS=15
  ATTEMPT=0
  HEALTH_OK=0

  while [[ ${ATTEMPT} -lt ${MAX_ATTEMPTS} ]]; do
    ATTEMPT=$((ATTEMPT + 1))
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/health || true)

    if [[ "${HTTP_CODE}" == "200" ]]; then
      HEALTH_OK=1
      break
    fi
    sleep 2
  done

  if [[ ${HEALTH_OK} -eq 1 ]]; then
    HEALTH_DATA=$(curl -s http://127.0.0.1:8000/api/health)
    log_success "Health check пройден! Ответ: ${HEALTH_DATA}"
  else
    log_warn "Health check не ответил 200 за отведенное время (код: ${HTTP_CODE})."
    log_warn "Проверьте логи командой: journalctl ${DEPLOY_MODE:+--user} -u ${APP_NAME} -f"
  fi
}

# ------------------------------------------------------------------------------
# Summary & Help
# ------------------------------------------------------------------------------
show_summary() {
  SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")

  echo ""
  echo -e "${C_GREEN}══════════════════════════════════════════════════════════════════${C_RESET}"
  echo -e "${C_BOLD}  🎉 Раскатка Torrent Media Manager v${APP_VERSION} завершена успешно!${C_RESET}"
  echo -e "${C_GREEN}══════════════════════════════════════════════════════════════════${C_RESET}"
  echo -e "  🌐 Web UI:         ${C_CYAN}http://${SERVER_IP}:8000/${C_RESET} или ${C_CYAN}http://localhost:8000/${C_RESET}"
  echo -e "  📁 Код приложения: ${C_YELLOW}${OPT_DIR}${C_RESET}"
  echo -e "  ⚙️  Конфигурация:   ${C_YELLOW}${ENV_FILE}${C_RESET}"
  echo -e "  📦 Quadlet Unit:   ${QUADLET_TARGET}"
  echo -e "  💾 База данных:    ${OPT_DIR}/data/app.db"
  echo ""
  if [[ -n "${ORIG_DIR:-}" && "${ORIG_DIR}" != "${OPT_DIR}" ]]; then
    echo -e "${C_BOLD}Очистка временных файлов:${C_RESET}"
    echo -e "  Код программы теперь находится в ${OPT_DIR}."
    echo -e "  Старую папку в root можно удалить командой: ${C_RED}rm -rf ${ORIG_DIR}${C_RESET}"
    echo ""
  fi
  echo -e "${C_BOLD}Полезные команды управления:${C_RESET}"
  echo -e "  • Статус сервиса:  ${C_YELLOW}${SYSTEMCTL_CMD} status ${APP_NAME}${C_RESET}"
  echo -e "  • Журнал логов:    ${C_YELLOW}journalctl ${DEPLOY_MODE:+--user} -u ${APP_NAME} -f${C_RESET}"
  echo -e "  • Перезапуск:      ${C_YELLOW}${SYSTEMCTL_CMD} restart ${APP_NAME}${C_RESET}"
  echo -e "  • Остановка:       ${C_YELLOW}${SYSTEMCTL_CMD} stop ${APP_NAME}${C_RESET}"
  echo -e "  • Обновление:      ${C_YELLOW}cd ${OPT_DIR} && git pull && ./deploy.sh${C_RESET}"
  echo -e "${C_GREEN}══════════════════════════════════════════════════════════════════${C_RESET}"
}

# ------------------------------------------------------------------------------
# Entrypoint & CLI arguments
# ------------------------------------------------------------------------------
main() {
  case "${1:-deploy}" in
    deploy|install)
      print_banner
      check_system
      load_user_ids
      ensure_canonical_location
      detect_mode
      setup_directories
      setup_config
      build_image
      install_quadlet
      start_service
      verify_health
      show_summary
      ;;
    status)
      detect_mode
      ${SYSTEMCTL_CMD} status "${APP_NAME}"
      ;;
    logs)
      detect_mode
      journalctl ${DEPLOY_MODE:+--user} -u "${APP_NAME}" -f
      ;;
    restart)
      detect_mode
      ${SYSTEMCTL_CMD} restart "${APP_NAME}"
      ;;
    stop)
      detect_mode
      ${SYSTEMCTL_CMD} stop "${APP_NAME}"
      ;;
    help|--help|-h)
      echo "Использование: $0 [deploy|status|logs|restart|stop]"
      ;;
    *)
      log_error "Неизвестная команда: $1"
      echo "Использование: $0 [deploy|status|logs|restart|stop]"
      exit 1
      ;;
  esac
}

main "$@"
