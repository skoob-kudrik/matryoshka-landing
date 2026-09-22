#!/usr/bin/env bash
# Сборка лендинга и публикация в Yandex Object Storage (S3-совместимый API).
#
# Ключи и параметры берутся из окружения. Локально они подхватываются из
# ~/secrets.env (секреты в код/репозиторий не пишем!). В CI — из GitHub Secrets.
#
# Нужные переменные:
#   YC_BUCKET             — имя бакета (например matryoshka-landing)
#   AWS_ACCESS_KEY_ID     — идентификатор статического ключа сервисного аккаунта YC
#   AWS_SECRET_ACCESS_KEY — секрет этого ключа
# Необязательные:
#   YC_KEY_ID / YC_SECRET — синонимы AWS_*, если так удобнее хранить в secrets.env
#   YC_CDN_RESOURCE_ID    — id CDN-ресурса; если задан, кэш будет сброшен (нужен yc cli)
set -euo pipefail

cd "$(dirname "$0")"

# локальный запуск: подтянуть секреты из ~/secrets.env
if [ -f "$HOME/secrets.env" ]; then
  set -a; . "$HOME/secrets.env"; set +a
fi

# позволяем хранить ключи под именами YC_KEY_ID / YC_SECRET
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${YC_KEY_ID:-}}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${YC_SECRET:-}}"

: "${YC_BUCKET:?укажите имя бакета в YC_BUCKET}"
: "${AWS_ACCESS_KEY_ID:?нужен ключ доступа (AWS_ACCESS_KEY_ID или YC_KEY_ID)}"
: "${AWS_SECRET_ACCESS_KEY:?нужен секрет (AWS_SECRET_ACCESS_KEY или YC_SECRET)}"

# aws может быть установлен через pip --user и не лежать в PATH
if ! command -v aws >/dev/null 2>&1; then
  for d in "$HOME"/Library/Python/*/bin "$HOME/.local/bin"; do
    [ -x "$d/aws" ] && PATH="$d:$PATH" && break
  done
fi
command -v aws >/dev/null 2>&1 || { echo "✗ не найден aws CLI (pip3 install --user awscli)"; exit 1; }

ENDPOINT="https://storage.yandexcloud.net"
REGION="ru-central1"
S3="aws --endpoint-url $ENDPOINT --region $REGION s3"

echo "▸ сборка страницы"
python3 build.py

echo "▸ заливка index.html"
$S3 cp index.html "s3://$YC_BUCKET/index.html" \
  --content-type "text/html; charset=utf-8" \
  --cache-control "no-cache, max-age=0"

echo "▸ синхронизация документов"
$S3 sync документы "s3://$YC_BUCKET/документы" \
  --content-type "application/pdf" \
  --cache-control "public, max-age=86400" \
  --exclude "*" --include "*.pdf" \
  --delete

if [ -n "${YC_CDN_RESOURCE_ID:-}" ]; then
  echo "▸ сброс кэша CDN"
  yc cdn cache purge --resource-id "$YC_CDN_RESOURCE_ID" --path '/*' \
    || echo "⚠ не удалось сбросить кэш CDN — обновится по TTL"
fi

echo "✓ опубликовано в s3://$YC_BUCKET"
