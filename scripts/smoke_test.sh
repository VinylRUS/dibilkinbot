#!/usr/bin/env bash
# Smoke-тест: запускает локальный сервер и тестирует endpoint'ы.
set -e

cd /home/z/my-project/kinovecher

# Удаляю старую БД для чистого теста
rm -f /tmp/bot_smoke.db

export APP_SECRET="MhrONHuG2rRffhs6k7Hf8uyUQHjXkQHWYTOwq3v-BhI="
export DATABASE_PATH="/tmp/bot_smoke.db"
export ADMIN_DISCORD_ID="123456789012345678"
export PORT=8765
export HOST=127.0.0.1

# Убиваю старый сервер если есть
pkill -f "port=8765\|port 8765" 2>/dev/null || true
sleep 1

# Запускаю сервер через nohup (живёт после выхода bash)
nohup /home/z/.venv/bin/python3 -c "
import asyncio, uvicorn
from web import app
asyncio.run(uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8765, log_level='warning')).serve())
" > /tmp/smoke_test.log 2>&1 < /dev/null &
disown

# Ждём пока поднимется
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -s -o /dev/null http://127.0.0.1:8765/login 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "=== Public endpoints ==="
echo "GET /login → $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/login)"
echo "GET /healthz → $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/healthz)"
echo "GET / (no auth) → $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/) [expect 303]"
echo "GET /api/wheel/items (no auth) → $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/api/wheel/items) [expect 303]"
echo "GET /download/test.txt → $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/download/test.txt) [expect 404 — route removed]"
echo "GET /api/filmnight/status → $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/api/filmnight/status) [expect 404 — route removed]"
echo "POST /api/filmnight/start → $(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8765/api/filmnight/start) [expect 404]"
echo "POST /api/filmnight/end → $(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8765/api/filmnight/end) [expect 404]"

echo ""
echo "=== Generate viewer session ==="
VIEWER_SESSION=$(/home/z/.venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from web import create_session
print(create_session({'discord_id': 443885447697924107, 'username': 'Poty', 'is_admin': False, 'current_guild_id': 0}))
" 2>/dev/null)
echo "viewer session len=${#VIEWER_SESSION}"

echo ""
echo "=== Viewer endpoints ==="
echo "GET / → $(curl -s -o /dev/null -b 'session=$VIEWER_SESSION' -w '%{http_code}' http://127.0.0.1:8765/) [expect 200]"
echo "GET /profile → $(curl -s -o /dev/null -b 'session=$VIEWER_SESSION' -w '%{http_code}' http://127.0.0.1:8765/profile) [expect 200]"
echo "GET /watched → $(curl -s -o /dev/null -b 'session=$VIEWER_SESSION' -w '%{http_code}' http://127.0.0.1:8765/watched) [expect 200]"
echo "GET /quotes → $(curl -s -o /dev/null -b 'session=$VIEWER_SESSION' -w '%{http_code}' http://127.0.0.1:8765/quotes) [expect 200]"
echo "GET /winners → $(curl -s -o /dev/null -b 'session=$VIEWER_SESSION' -w '%{http_code}' http://127.0.0.1:8765/winners) [expect 200]"
echo "GET /tokens (viewer → 403) → $(curl -s -o /dev/null -b 'session=$VIEWER_SESSION' -w '%{http_code}' http://127.0.0.1:8765/tokens) [expect 403]"
echo "GET /api/wheel/items → $(curl -s -b 'session=$VIEWER_SESSION' -w ' [%{http_code}]' http://127.0.0.1:8765/api/wheel/items) [expect 200 + JSON]"

echo ""
echo "=== Generate admin session ==="
ADMIN_SESSION=$(/home/z/.venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from web import create_session
print(create_session({'discord_id': 0, 'username': 'admin', 'is_admin': True, 'current_guild_id': 0}))
" 2>/dev/null)

echo ""
echo "=== Admin endpoints ==="
echo "GET /tokens (admin) → $(curl -s -o /dev/null -b 'session=$ADMIN_SESSION' -w '%{http_code}' http://127.0.0.1:8765/tokens) [expect 200]"
echo "GET /users (admin) → $(curl -s -o /dev/null -b 'session=$ADMIN_SESSION' -w '%{http_code}' http://127.0.0.1:8765/users) [expect 200]"
echo "GET /guilds (admin) → $(curl -s -o /dev/null -b 'session=$ADMIN_SESSION' -w '%{http_code}' http://127.0.0.1:8765/guilds) [expect 200]"
echo "GET /channels (admin) → $(curl -s -o /dev/null -b 'session=$ADMIN_SESSION' -w '%{http_code}' http://127.0.0.1:8765/channels) [expect 200]"
echo "GET /features (admin) → $(curl -s -o /dev/null -b 'session=$ADMIN_SESSION' -w '%{http_code}' http://127.0.0.1:8765/features) [expect 200]"
echo "GET /wheel (any user) → $(curl -s -o /dev/null -b 'session=$VIEWER_SESSION' -w '%{http_code}' http://127.0.0.1:8765/wheel) [expect 200]"

echo ""
echo "=== Server log (last 10 lines) ==="
tail -10 /tmp/smoke_test.log

echo ""
echo "DONE"
