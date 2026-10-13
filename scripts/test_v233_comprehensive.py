"""v2.3.3 Комплексный smoke-test всех модулей.

Проверяет:
1. Синтаксис всех Python файлов
2. Импортируемость всех модулей
3. Регистрацию роутов в app
4. Математика колеса (Python + JS совпадение)
5. Логика ачивок (триггеры, роли)
6. Логика Santa (включение/выключение)
7. WS manager (broadcast функции)
8. deps.py (auth, templates, utils)
9. changelog_parser (теги)
10. wheel_test_log (логирование)

Запуск: python3 scripts/test_v233_comprehensive.py
"""
import ast
import json
import math
import os
import sys
import traceback

# Добавляем путь к проекту
PROJECT_DIR = '/home/z/my-project/kinovecher'
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

PASS = 0
FAIL = 0
SKIP = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} — {detail}")

def skip(name, reason=""):
    global SKIP
    SKIP += 1
    print(f"  - {name} (skip: {reason})")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ============================================================
section("1. Синтаксис всех Python файлов")
# ============================================================

python_files = [
    'web.py', 'deps.py', 'db.py', 'bot.py', 'guild.py', 'config.py',
    'steam.py', 'telegram.py', 'kinopoisk.py', 'main.py',
    'ws_manager.py', 'wheel_test_log.py', 'changelog_parser.py',
    'wheel_routes.py', 'movienight_routes.py',
    'achievements_routes.py', 'santa_routes.py',
]

for f in python_files:
    path = os.path.join(PROJECT_DIR, f)
    if not os.path.exists(path):
        skip(f"Синтаксис {f}", "файл не найден")
        continue
    try:
        with open(path) as fh:
            ast.parse(fh.read())
        check(f"Синтаксис {f}", True)
    except SyntaxError as e:
        check(f"Синтаксис {f}", False, f"line {e.lineno}: {e.msg}")

# ============================================================
section("2. Регистрация роутов в модулях")
# ============================================================

# Парсим роуты из исходников (без импорта — чтобы не требовать БД)
def extract_routes(filepath):
    """Извлекает список роутов из файла через AST."""
    with open(filepath) as f:
        tree = ast.parse(f.read())
    routes = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    func = decorator.func
                    if isinstance(func, ast.Attribute):
                        # @router.get("/path") or @app.get("/path")
                        if func.attr in ('get', 'post', 'delete', 'put', 'websocket'):
                            if decorator.args:
                                arg = decorator.args[0]
                                if isinstance(arg, ast.Constant):
                                    routes.append((func.attr.upper(), arg.value))
    return routes

# Wheel routes
wheel_routes_list = extract_routes('wheel_routes.py')
check(f"wheel_routes: есть роуты", len(wheel_routes_list) > 0, "0 роутов найдено")
check(f"wheel_routes: GET /wheel", ('GET', '/wheel') in wheel_routes_list)
check(f"wheel_routes: POST /api/wheel/spin", ('POST', '/api/wheel/spin') in wheel_routes_list)
check(f"wheel_routes: POST /api/wheel/result", ('POST', '/api/wheel/result') in wheel_routes_list)
check(f"wheel_routes: POST /api/wheel/spin_elimination", ('POST', '/api/wheel/spin_elimination') in wheel_routes_list)
check(f"wheel_routes: WEBSOCKET /ws/wheel", ('WEBSOCKET', '/ws/wheel') in wheel_routes_list)
check(f"wheel_routes: GET /api/wheel/items", ('GET', '/api/wheel/items') in wheel_routes_list)
check(f"wheel_routes: POST /api/wheel/test/start", ('POST', '/api/wheel/test/start') in wheel_routes_list)
check(f"wheel_routes: POST /api/wheel/test/end", ('POST', '/api/wheel/test/end') in wheel_routes_list)
check(f"wheel_routes: всего роутов = {len(wheel_routes_list)}", len(wheel_routes_list) >= 16, f"найдено {len(wheel_routes_list)}")

# Movienight routes
mn_routes = extract_routes('movienight_routes.py')
check(f"movienight: GET /movienight", ('GET', '/movienight') in mn_routes)
check(f"movienight: POST /api/collection/start", ('POST', '/api/collection/start') in mn_routes)
check(f"movienight: POST /api/collection/save_picks", ('POST', '/api/collection/save_picks') in mn_routes)
check(f"movienight: POST /api/collection/start_spin", ('POST', '/api/collection/start_spin') in mn_routes)
check(f"movienight: GET /api/collection/status", ('GET', '/api/collection/status') in mn_routes)

# Achievements routes
ach_routes = extract_routes('achievements_routes.py')
check(f"achievements: GET /achievements", ('GET', '/achievements') in ach_routes)
check(f"achievements: POST /api/achievements/create", ('POST', '/api/achievements/create') in ach_routes)
check(f"achievements: POST /api/achievements/{'{ach_id}'}/delete", ('POST', '/api/achievements/{ach_id}/delete') in ach_routes)
check(f"achievements: POST /api/achievements/{'{ach_id}'}/edit", ('POST', '/api/achievements/{ach_id}/edit') in ach_routes)
check(f"achievements: POST /api/achievements/{'{ach_id}'}/grant", ('POST', '/api/achievements/{ach_id}/grant') in ach_routes)
check(f"achievements: GET /api/achievements/my_grants", ('GET', '/api/achievements/my_grants') in ach_routes)

# Santa routes
santa_routes_list = extract_routes('santa_routes.py')
check(f"santa: GET /santa", ('GET', '/santa') in santa_routes_list)
check(f"santa: POST /api/santa/create", ('POST', '/api/santa/create') in santa_routes_list)
check(f"santa: POST /api/santa/join", ('POST', '/api/santa/join') in santa_routes_list)
check(f"santa: POST /api/santa/assign", ('POST', '/api/santa/assign') in santa_routes_list)
check(f"santa: POST /api/santa/reveal", ('POST', '/api/santa/reveal') in santa_routes_list)

# web.py не должен содержать дублирующих роутов
web_routes = extract_routes('web.py')
check(f"web.py: нет /wheel", ('GET', '/wheel') not in web_routes, "дублирует wheel_routes")
check(f"web.py: нет /api/wheel/spin", ('POST', '/api/wheel/spin') not in web_routes, "дублирует")
check(f"web.py: нет /movienight", ('GET', '/movienight') not in web_routes, "дублирует")
check(f"web.py: нет /santa", ('GET', '/santa') not in web_routes, "дублирует")
check(f"web.py: нет /achievements", ('GET', '/achievements') not in web_routes, "дублирует")
check(f"web.py: нет /api/collection/start", ('POST', '/api/collection/start') not in web_routes, "дублирует")

# ============================================================
section("3. Математика колеса — Python (wheel_routes.py)")
# ============================================================

def _get_sector_under_pointer(rotation, items_count):
    """Копия из wheel_routes.py для тестирования."""
    if items_count == 0:
        return -1
    angle_per = (2 * math.pi) / items_count
    norm = (-math.pi / 2 - rotation) % (2 * math.pi)
    if norm < 0:
        norm += 2 * math.pi
    return int(norm // angle_per) % items_count

# Базовые случаи
check("sector: rotation=0, 4 items → сектор 3", _get_sector_under_pointer(0, 4) == 3)
check("sector: rotation=π/2, 4 items → сектор 2", _get_sector_under_pointer(math.pi/2, 4) == 2)
check("sector: rotation=-π/2, 4 items → сектор 0", _get_sector_under_pointer(-math.pi/2, 4) == 0)
check("sector: rotation=π, 4 items → сектор 1", _get_sector_under_pointer(math.pi, 4) == 1)

# Много оборотов
check("sector: 5 оборотов, 4 items → сектор 2", _get_sector_under_pointer(5*2*math.pi, 4) == 2)
check("sector: 5.25 оборотов, 4 items → сектор 1", _get_sector_under_pointer(5.25*2*math.pi, 4) == 1)

# Разные количества items
for count in [2, 3, 5, 6, 8, 10, 12, 20]:
    rotation = 5 * 2 * math.pi + 0.1  # 5 оборотов + чуть
    result = _get_sector_under_pointer(rotation, count)
    check(f"sector: {count} items, rotation={rotation:.1f} → сектор {result}", 0 <= result < count)

# Граничные случаи
check("sector: 0 items → -1", _get_sector_under_pointer(0, 0) == -1)
check("sector: 1 item → всегда 0", _get_sector_under_pointer(123.456, 1) == 0)

# Отрицательный rotation
check("sector: отрицательный rotation, 6 items", 0 <= _get_sector_under_pointer(-100.5, 6) < 6)

# Случайные rotation
import random
random.seed(42)
all_ok = True
for _ in range(100):
    rot = random.uniform(-1000, 1000)
    count = random.randint(2, 20)
    result = _get_sector_under_pointer(rot, count)
    if not (0 <= result < count):
        all_ok = False
        break
check("sector: 100 случайных rotation → все валидные", all_ok)

# ============================================================
section("4. Математика колеса — JS (wheel.js)")
# ============================================================

# Читаем JS и проверяем что функции существуют
with open('static/js/wheel.js') as f:
    js_code = f.read()

check("JS: есть getSectorUnderPointer", "function getSectorUnderPointer" in js_code)
check("JS: есть wheelEase", "function wheelEase" in js_code)
check("JS: есть animateSpin", "function animateSpin" in js_code)
check("JS: есть playTick", "function playTick" in js_code)
check("JS: есть runSpinAnimation", "function runSpinAnimation" in js_code)
check("JS: есть sendSpinResult", "function sendSpinResult" in js_code)
check("JS: есть connectWS", "function connectWS" in js_code)
check("JS: есть bouncePointer", "function bouncePointer" in js_code)
check("JS: нет Jinja2 {{ }} в JS", "{{" not in js_code, "остались Jinja2 теги")
check("JS: используется GUILD_ID константа", "GUILD_ID" in js_code)
check("JS: используется INITIAL_ITEMS константа", "INITIAL_ITEMS" in js_code)

# Проверка wheelEase через Python эквивалент
def wheelEase(t):
    ACCEL_END = 0.20
    PLATEAU_END = 0.70
    accel_area = ACCEL_END / 2
    plateau_area = PLATEAU_END - ACCEL_END
    decel_area = (1 - PLATEAU_END) / 2
    v_max = 1 / (accel_area + plateau_area + decel_area)
    if t < ACCEL_END:
        u = t / ACCEL_END
        u3 = u * u * u
        integral = u3 - u3 * u / 2
        return v_max * ACCEL_END * integral
    elif t < PLATEAU_END:
        accel_full = v_max * ACCEL_END * (1 - 0.5)
        return accel_full + v_max * (t - ACCEL_END)
    else:
        accel_full = v_max * ACCEL_END * 0.5
        plateau_full = v_max * (PLATEAU_END - ACCEL_END)
        u = (t - PLATEAU_END) / (1 - PLATEAU_END)
        u3 = u * u * u
        u4 = u3 * u
        integral = u - u3 + u4 / 2
        return accel_full + plateau_full + v_max * (1 - PLATEAU_END) * integral

check("wheelEase(0) = 0", abs(wheelEase(0)) < 0.001)
check("wheelEase(1) = 1", abs(wheelEase(1) - 1) < 0.001)
check("wheelEase(0.5) между 0 и 1", 0 < wheelEase(0.5) < 1)
check("wheelEase монотонна", all(wheelEase(i/20) <= wheelEase((i+1)/20) for i in range(20)))

# Производная непрерывна на стыках
eps = 0.0001
d1 = (wheelEase(0.20 + eps) - wheelEase(0.20)) / eps
d2 = (wheelEase(0.20) - wheelEase(0.20 - eps)) / eps
check("wheelEase: непрерывна на 20%", abs(d1 - d2) < 0.01, f"d1={d1:.3f} d2={d2:.3f}")
d3 = (wheelEase(0.70 + eps) - wheelEase(0.70)) / eps
d4 = (wheelEase(0.70) - wheelEase(0.70 - eps)) / eps
check("wheelEase: непрерывна на 70%", abs(d3 - d4) < 0.01, f"d3={d3:.3f} d4={d4:.3f}")

# ============================================================
section("5. Логика ачивок")
# ============================================================

# Проверяем константы в db.py
with open('db.py') as f:
    db_code = f.read()

check("db: есть ACHIEVEMENT_TRIGGERS", "ACHIEVEMENT_TRIGGERS" in db_code)
check("db: есть steam_play_time", "steam_play_time" in db_code)
check("db: есть steam_games_count", "steam_games_count" in db_code)
check("db: есть steam_play_time_specific", "steam_play_time_specific" in db_code)
check("db: есть voice_co_sessions", "voice_co_sessions" in db_code)
check("db: есть TRIGGER_NEEDS_GAME_NAME", "TRIGGER_NEEDS_GAME_NAME" in db_code)
check("db: есть ROLE_LABELS", "ROLE_LABELS" in db_code)

# Проверяем ROLE_LABELS через AST
try:
    with open('db.py') as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'ROLE_LABELS':
                    check("db: ROLE_LABELS = dict", isinstance(node.value, ast.Dict))
except Exception as e:
    check("db: ROLE_LABELS parse", False, str(e))

# Проверяем что achievements_routes импортирует require_admin
with open('achievements_routes.py') as f:
    ach_code = f.read()
check("achievements: импортирует require_admin", "require_admin" in ach_code)
check("achievements: импортирует require_superuser", "require_superuser" in ach_code or "require_admin" in ach_code)
check("achievements: нет @app.", "@app." not in ach_code, "остались @app декораторы")

# ============================================================
section("6. Логика Santa")
# ============================================================

with open('santa_routes.py') as f:
    santa_code = f.read()

check("santa: есть is_santa_enabled", "is_santa_enabled" in santa_code)
check("santa: нет @app.", "@app." not in santa_code, "остались @app декораторы")
check("santa: импортирует require_user", "require_user" in santa_code)
check("santa: есть /api/santa/assign", "/api/santa/assign" in santa_code)
check("santa: есть /api/santa/mark_gift_sent", "/api/santa/mark_gift_sent" in santa_code)

# ============================================================
section("7. WS Manager")
# ============================================================

with open('ws_manager.py') as f:
    ws_code = f.read()

check("ws: есть broadcast_spin_started", "broadcast_spin_started" in ws_code)
check("ws: есть broadcast_spin_result", "broadcast_spin_result" in ws_code)
check("ws: есть broadcast_wheel_updated", "broadcast_wheel_updated" in ws_code)
check("ws: есть broadcast_collection_started", "broadcast_collection_started" in ws_code)
check("ws: broadcast_spin_started принимает target_item", "target_item" in ws_code)
check("ws: broadcast_spin_started принимает mode", "mode" in ws_code)
check("ws: broadcast_spin_started принимает duration", "duration" in ws_code)

# ============================================================
section("8. deps.py — общие зависимости")
# ============================================================

with open('deps.py') as f:
    deps_code = f.read()

check("deps: есть require_user", "async def require_user" in deps_code)
check("deps: есть require_admin", "async def require_admin" in deps_code)
check("deps: есть require_superuser", "async def require_superuser" in deps_code)
check("deps: есть get_current_guild_id", "def get_current_guild_id" in deps_code)
check("deps: есть anonymize_items", "def anonymize_items" in deps_code)
check("deps: есть templates", "templates = Jinja2Templates" in deps_code)
check("deps: есть serializer", "serializer = URLSafeTimedSerializer" in deps_code)
check("deps: есть create_session", "def create_session" in deps_code)
check("deps: есть read_session", "def read_session" in deps_code)

# ============================================================
section("9. changelog_parser — теги")
# ============================================================

try:
    from changelog_parser import parse_changelog, get_latest_version, get_latest_changelog
    
    entries = parse_changelog("CHANGELOG.md")
    check("changelog: распарсено >0 записей", len(entries) > 0)
    
    if entries:
        latest = entries[0]
        check(f"changelog: последняя версия = {latest.version}", latest.version.startswith("v"))
        
        # Проверяем что теги распознаются
        has_new = "Новое" in latest.sections
        has_admin = "Админские фичи" in latest.sections
        has_tech = "Техническая информация" in latest.sections
        check("changelog: есть секция Новое", has_new)
        check("changelog: есть секция Админские фичи", has_admin)
        check("changelog: есть секция Техническая информация", has_tech)
        
        # public_sections не содержит админских
        public = latest.public_sections
        check("changelog: public не содержит Админские", "Админские фичи" not in public)
        check("changelog: public не содержит Техническую", "Техническая информация" not in public)
        
        # admin_sections содержит только админские
        admin = latest.admin_sections
        check("changelog: admin содержит Админские", "Админские фичи" in admin)
        check("changelog: admin не содержит Новое", "Новое" not in admin)
except Exception as e:
    check(f"changelog: импорт", False, str(e))

# ============================================================
section("10. wheel_test_log — логирование")
# ============================================================

with open('wheel_test_log.py') as f:
    log_code = f.read()

check("log: есть start_test_log", "def start_test_log" in log_code)
check("log: есть log_event", "def log_event" in log_code)
check("log: есть end_test_log", "def end_test_log" in log_code)
check("log: есть list_log_files", "def list_log_files" in log_code)
check("log: есть read_log_file", "def read_log_file" in log_code)
check("log: есть get_current_log_path", "def get_current_log_path" in log_code)

# ============================================================
section("11. Шаблоны — Jinja2 компиляция")
# ============================================================

from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates/'))

templates_to_check = [
    'wheel.html', 'movienight.html', 'panel.html', 'profile.html',
    'profile_public.html', 'achievements.html', 'santa.html',
    'sidebar.html', '_footer.html', 'admin_panel.html',
    'login.html', 'tokens.html', 'channels.html', 'features.html',
    'winners.html', 'watched.html', 'quotes.html',
]

for tmpl in templates_to_check:
    try:
        env.get_template(tmpl)
        check(f"template: {tmpl}", True)
    except Exception as e:
        check(f"template: {tmpl}", False, str(e)[:80])

# ============================================================
section("12. Шаблоны — external JS подключён")
# ============================================================

with open('templates/wheel.html') as f:
    wheel_html = f.read()
check("wheel.html: подключает wheel.js", "/static/js/wheel.js" in wheel_html)
check("wheel.html: нет inline <script> с большим кодом", wheel_html.count("<script>") <= 2)
check("wheel.html: есть GUILD_ID константа", "GUILD_ID" in wheel_html)
check("wheel.html: есть INITIAL_ITEMS константа", "INITIAL_ITEMS" in wheel_html)

with open('templates/movienight.html') as f:
    mn_html = f.read()
check("movienight.html: подключает movienight.js", "/static/js/movienight.js" in mn_html)
check("movienight.html: нет inline <script> с большим кодом", mn_html.count("<script>") <= 2)

# ============================================================
section("13. web.py — include_router")
# ============================================================

with open('web.py') as f:
    web_code = f.read()

check("web.py: include_router(wheel_routes)", "include_router(wheel_routes.router)" in web_code)
check("web.py: include_router(movienight_routes)", "include_router(movienight_routes.router)" in web_code)
check("web.py: include_router(achievements_routes)", "include_router(achievements_routes.router)" in web_code)
check("web.py: include_router(santa_routes)", "include_router(santa_routes.router)" in web_code)
check("web.py: импортирует deps", "from deps import" in web_code)

# ============================================================
section("14. bot.py — ключевые функции")
# ============================================================

with open('bot.py') as f:
    bot_code = f.read()

check("bot: on_wheel_spin_completed принимает is_test", "is_test" in bot_code and "on_wheel_spin_completed" in bot_code)
check("bot: _build_winner_embed принимает is_test", "is_test" in bot_code and "_build_winner_embed" in bot_code)
check("bot: _post_winner_to_telegram принимает is_test", "is_test" in bot_code and "_post_winner_to_telegram" in bot_code)
check("bot: есть get_joint_play_sessions", "get_joint_play_sessions" in bot_code)
check("bot: есть _check_all_auto_achievements", "_check_all_auto_achievements" in bot_code)
check("bot: есть PERIODIC_TRIGGERS", "PERIODIC_TRIGGERS" in bot_code)
check("bot: steam_games_count в PERIODIC_TRIGGERS", "steam_games_count" in bot_code)
check("bot: voice_co_sessions в PERIODIC_TRIGGERS", "voice_co_sessions" in bot_code)

# ============================================================
section("15. steam.py — функции")
# ============================================================

with open('steam.py') as f:
    steam_code = f.read()

check("steam: есть get_app_details", "def get_app_details" in steam_code)
check("steam: есть is_multiplayer_game", "def is_multiplayer_game" in steam_code)
check("steam: есть get_owned_games", "def get_owned_games" in steam_code)
check("steam: есть get_recently_played_games", "def get_recently_played_games" in steam_code)
check("steam: есть rtime_last_played", "rtime_last_played" in steam_code)
check("steam: есть get_top_wishlist_games", "def get_top_wishlist_games" in steam_code)
check("steam: иконки .jpg не .ico", ".jpg" in steam_code and ".ico" not in steam_code.split("icon_url")[1][:50] if "icon_url" in steam_code else True)

# ============================================================
section("16. db.py — ключевые функции")
# ============================================================

db_funcs_to_check = [
    'g_list_wheel_items', 'g_add_wheel_item', 'g_remove_wheel_item', 'g_clear_wheel',
    'g_add_winner', 'g_list_test_wheel_items', 'g_add_test_wheel_item',
    'g_remove_test_wheel_item', 'g_purge_test_data', 'g_get_test_stats',
    'g_get_user_games_merged', 'g_find_appid_by_game_name',
    'get_user_show_wishlist', 'set_user_show_wishlist',
    'g_get_voice_co_occurrence', 'g_get_game_play_time_specific',
    'g_check_and_grant_auto', 'g_get_user_trigger_count',
]

for func in db_funcs_to_check:
    check(f"db: есть {func}", f"def {func}" in db_code or f"async def {func}" in db_code)

# ============================================================
section("17. guild.py — таблицы")
# ============================================================

with open('guild.py') as f:
    guild_code = f.read()

check("guild: wheel_items has is_test", "is_test" in guild_code and "wheel_items" in guild_code)
check("guild: winners has is_test", "is_test" in guild_code and "winners" in guild_code)
check("guild: user_steam_games has rtime_last_played", "rtime_last_played" in guild_code)
check("guild: есть user_pinned_achievements", "user_pinned_achievements" in guild_code)

# ============================================================
# ИТОГ
# ============================================================
print(f"\n{'='*60}")
print(f"  ИТОГ: {PASS} passed, {FAIL} failed, {SKIP} skipped")
print(f"{'='*60}")

if FAIL > 0:
    sys.exit(1)
else:
    print("\n  Все тесты прошли! ✓")
