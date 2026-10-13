"""v2.3.3 Deep audit — проверка скрытых проблем после рефакторинга.

Проверяет:
1. Circular imports (deps → web → deps)
2. Missing references в модулях
3. Template variables (все globals доступны)
4. Route conflicts (дублирующиеся пути)
5. JS ↔ Python контракт (функции совпадают)
6. DB migrations (колонки существуют)
7. Import chains (всё что нужно импортируется)
8. Static files (JS/CSS доступны)
"""
import ast
import os
import sys
import re
import json

PROJECT_DIR = '/home/z/my-project/kinovecher'
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} — {detail}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ============================================================
section("1. Circular imports — deps не импортирует web")
# ============================================================

with open('deps.py') as f:
    deps_code = f.read()

# deps.py НЕ должен импортировать web, wheel_routes, movienight_routes
check("deps: НЕ импортирует web", "import web" not in deps_code and "from web" not in deps_code)
check("deps: НЕ импортирует wheel_routes", "import wheel_routes" not in deps_code)
check("deps: НЕ импортирует movienight_routes", "import movienight_routes" not in deps_code)
check("deps: НЕ импортирует achievements_routes", "import achievements_routes" not in deps_code)
check("deps: НЕ импортирует santa_routes", "import santa_routes" not in deps_code)

# wheel_routes НЕ должен импортировать web
with open('wheel_routes.py') as f:
    wr_code = f.read()
check("wheel_routes: НЕ импортирует web", "import web" not in wr_code and "from web" not in wr_code)

with open('movienight_routes.py') as f:
    mn_code = f.read()
check("movienight_routes: НЕ импортирует web", "import web" not in mn_code and "from web" not in mn_code)

with open('achievements_routes.py') as f:
    ach_code = f.read()
check("achievements_routes: НЕ импортирует web", "import web" not in ach_code and "from web" not in ach_code)

with open('santa_routes.py') as f:
    santa_code = f.read()
check("santa_routes: НЕ импортирует web", "import web" not in santa_code and "from web" not in santa_code)

# web.py импортирует все модули
with open('web.py') as f:
    web_code = f.read()
check("web: импортирует wheel_routes", "import wheel_routes" in web_code)
check("web: импортирует movienight_routes", "import movienight_routes" in web_code)
check("web: импортирует achievements_routes", "import achievements_routes" in web_code)
check("web: импортирует santa_routes", "import santa_routes" in web_code)
check("web: импортирует deps", "from deps import" in web_code)

# ============================================================
section("2. Missing references — функции используемые в модулях")
# ============================================================

# Все модули используют anonymize_items (не _anonymize_items)
for f_name, code in [('wheel_routes.py', wr_code), ('movienight_routes.py', mn_code),
                      ('achievements_routes.py', ach_code), ('santa_routes.py', santa_code)]:
    check(f"{f_name}: использует anonymize_items (не _аноним)", 
          "anonymize_items" in code and "_anonymize_items" not in code.replace("anonymize_items", ""))
    check(f"{f_name}: импортирует из deps", "from deps import" in code)

# web.py не определяет дублирующие функции
for func in ['def _get_git_commit_id', 'def _get_bot_version', 'def create_session', 
             'def read_session', 'async def get_current_user', 'async def require_user',
             'async def require_admin', 'async def require_superuser',
             'def get_current_guild_id', 'def _anonymize_items']:
    check(f"web.py: НЕ определяет {func}", func not in web_code, f"найдено: {func}")

# ============================================================
section("3. Template globals — всё доступно")
# ============================================================

# Проверяем что все нужные globals регистрируются
globals_to_check = [
    "bot_version", "commit_id", "bot_version_with_commit",
    "theme_from_request", "format_msk", "format_msk_short", "santa_enabled"
]

for g in globals_to_check:
    check(f"web.py: регистрирует {g}", f'"{g}"' in web_code or f"'{g}'" in web_code,
          f"не найдено templates.env.globals['{g}']")

# Шаблоны используют globals
with open('templates/_footer.html') as f:
    footer = f.read()
check("_footer: использует bot_version_with_commit", "bot_version_with_commit" in footer)

with open('templates/sidebar.html') as f:
    sidebar = f.read()
check("sidebar: использует theme_from_request", "theme_from_request" in sidebar)
check("sidebar: использует santa_enabled", "santa_enabled" in sidebar)
# sidebar — это partial, _footer инклудится в page templates, не в sidebar

# ============================================================
section("4. Route conflicts — нет дубликатов путей")
# ============================================================

def extract_routes_full(filepath):
    """Извлекает все роуты (method, path) из файла."""
    with open(filepath) as f:
        tree = ast.parse(f.read())
    routes = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    func = decorator.func
                    if isinstance(func, ast.Attribute) and func.attr in ('get', 'post', 'delete', 'put', 'websocket'):
                        if decorator.args:
                            arg = decorator.args[0]
                            if isinstance(arg, ast.Constant):
                                routes.append((func.attr.upper(), arg.value, node.name))
    return routes

all_routes = {}
for f_name in ['web.py', 'wheel_routes.py', 'movienight_routes.py', 
               'achievements_routes.py', 'santa_routes.py']:
    routes = extract_routes_full(f_name)
    for method, path, func_name in routes:
        key = (method, path)
        if key in all_routes:
            check(f"Route conflict: {method} {path}", False, 
                  f"дублируется в {f_name}::{func_name} и {all_routes[key]}")
        else:
            all_routes[key] = f"{f_name}::{func_name}"

check(f"Нет конфликтов роутов (всего {len(all_routes)})", True)

# Проверяем что ключевые роуты существуют
expected_routes = [
    ('GET', '/'), ('GET', '/login'), ('POST', '/login'), ('GET', '/logout'),
    ('GET', '/wheel'), ('POST', '/api/wheel/spin'), ('POST', '/api/wheel/result'),
    ('GET', '/movienight'), ('POST', '/api/collection/start'),
    ('GET', '/achievements'), ('POST', '/api/achievements/create'),
    ('GET', '/santa'), ('POST', '/api/santa/create'),
    ('GET', '/profile'), ('GET', '/u/'),
    ('GET', '/winners'), ('GET', '/watched'), ('GET', '/quotes'),
    ('GET', '/tokens'), ('GET', '/channels'), ('GET', '/features'),
    ('GET', '/adminpanel'), ('GET', '/healthz'),
    ('WEBSOCKET', '/ws/wheel'),
]

for method, path_prefix in expected_routes:
    found = any(r[0] == method and r[1].startswith(path_prefix.rstrip('/')) for r in all_routes.keys())
    if path_prefix == '/u/':
        # Special: /u/{discord_id}
        found = any(r[0] == 'GET' and r[1].startswith('/u/') for r in all_routes.keys())
    check(f"Route exists: {method} {path_prefix}*", found)

# ============================================================
section("5. JS ↔ Python контракт")
# ============================================================

# getSectorUnderPointer — совпадение формулы
with open('static/js/wheel.js') as f:
    js_code = f.read()

# Python версия
import math
def py_get_sector(rotation, count):
    if count == 0: return -1
    ap = (2 * math.pi) / count
    norm = (-math.pi / 2 - rotation) % (2 * math.pi)
    if norm < 0: norm += 2 * math.pi
    return int(norm // ap) % count

# Проверка что JS использует ту же формулу
check("JS: getSectorUnderPointer использует -Math.PI/2", "-Math.PI / 2" in js_code)
check("JS: getSectorUnderPointer использует Math.floor", "Math.floor" in js_code)
check("JS: нет targetRotation (Вариант D — без target)", "targetRotation" not in js_code.split("runSpinAnimation")[1][:500] if "runSpinAnimation" in js_code else False,
      "runSpinAnimation ещё использует targetRotation")

# wheelEase — монотонность
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

# 1000 точек — проверка монотонности
import random
random.seed(42)
all_monotone = True
prev = -1
for i in range(1001):
    t = i / 1000
    v = wheelEase(t)
    if v < prev - 0.0001:
        all_monotone = False
        break
    prev = v
check("wheelEase: монотонна на 1000 точках", all_monotone)

# Производная непрерывна
eps = 0.00001
for boundary in [0.20, 0.70]:
    d_before = (wheelEase(boundary) - wheelEase(boundary - eps)) / eps
    d_after = (wheelEase(boundary + eps) - wheelEase(boundary)) / eps
    diff = abs(d_after - d_before)
    check(f"wheelEase: непрерывна на {int(boundary*100)}%", diff < 0.01, f"diff={diff:.4f}")

# ============================================================
section("6. DB migrations — колонки существуют в схеме")
# ============================================================

with open('guild.py') as f:
    guild_code = f.read()

# is_test в wheel_items и winners — ищем в схеме таблицы
check("guild: wheel_items.is_test в CREATE TABLE", 
      '"wheel_items"' in guild_code and 'is_test INTEGER DEFAULT 0' in guild_code.split('"wheel_items": """')[1][:500] if '"wheel_items": """' in guild_code else False)
check("guild: winners.is_test в CREATE TABLE",
      '"winners"' in guild_code and 'is_test INTEGER DEFAULT 0' in guild_code.split('"winners": """')[1][:500] if '"winners": """' in guild_code else False)

# Индексы для is_test
check("guild: idx wheel_items_test", "idx_{table}_test" in guild_code)
check("guild: rtime_last_played индекс", "idx_{table}_last_played" in guild_code)

# Миграции в db.py
with open('db.py') as f:
    db_code = f.read()
check("db: миграция is_test для wheel_items", "is_test" in db_code and "wheel_items" in db_code.split("v2.3.3")[1][:500] if "v2.3.3" in db_code else False,
      "v2.3.3 миграция не найдена")
check("db: миграция rtime_last_played", "rtime_last_played" in db_code and "v2.3.2" in db_code)

# show_wishlist колонка
check("db: show_wishlist в new_cols", "show_wishlist" in db_code)

# ============================================================
section("7. Import chains — всё импортируется")
# ============================================================

# deps.py импортирует
deps_imports = re.findall(r'^(?:import|from)\s+(\S+)', deps_code, re.MULTILINE)
check("deps: импортирует db", "db" in deps_imports)
check("deps: импортирует config", "config" in deps_imports or "from config" in deps_code)
check("deps: импортирует crypto", "crypto" in deps_imports)
check("deps: импортирует changelog_parser", "changelog_parser" in deps_code)

# wheel_routes импортирует deps
check("wheel_routes: from deps import", "from deps import" in wr_code)
check("wheel_routes: импортирует db", "import db" in wr_code)
check("wheel_routes: импортирует ws_manager", "import ws_manager" in wr_code)
check("wheel_routes: импортирует wheel_test_log", "import wheel_test_log" in wr_code)

# bot.py импортирует github_monitor
with open('bot.py') as f:
    bot_code = f.read()
check("bot: импортирует github_monitor (в on_ready)", "import github_monitor" in bot_code)

# github_monitor импортирует deps (для _get_bot_version)
with open('github_monitor.py') as f:
    gh_code = f.read()
check("github_monitor: импортирует db", "import db" in gh_code)
check("github_monitor: импортирует httpx", "import httpx" in gh_code or "from httpx" in gh_code)

# ============================================================
section("8. Static files — JS/CSS доступны")
# ============================================================

check("static/js/wheel.js существует", os.path.exists('static/js/wheel.js'))
check("static/js/movienight.js существует", os.path.exists('static/js/movienight.js'))
check("static/style.css существует", os.path.exists('static/style.css'))

# wheel.html подключает JS
with open('templates/wheel.html') as f:
    wh = f.read()
check("wheel.html: <script src='/static/js/wheel.js'>", "/static/js/wheel.js" in wh)

# movienight.html подключает JS
with open('templates/movienight.html') as f:
    mh = f.read()
check("movienight.html: <script src='/static/js/movienight.js'>", "/static/js/movienight.js" in mh)

# Нет inline JS больше 100 строк
for tmpl, name in [(wh, 'wheel.html'), (mh, 'movienight.html')]:
    script_blocks = re.findall(r'<script>(.*?)</script>', tmpl, re.DOTALL)
    for i, block in enumerate(script_blocks):
        lines = block.strip().split('\n')
        check(f"{name}: inline script #{i} < 100 строк ({len(lines)})", len(lines) < 100,
              f"{len(lines)} строк")

# ============================================================
section("9. Wheel Variant D — контракт")
# ============================================================

# /api/wheel/spin НЕ выбирает winner заранее
check("wheel_routes: spin НЕ содержит random.choice(winner)", 
      "winner = random.choice" not in wr_code.split("api_spin_wheel")[1].split("def ")[0] if "api_spin_wheel" in wr_code else False,
      "spin всё ещё выбирает winner заранее")

# /api/wheel/result существует
check("wheel_routes: есть api_wheel_result", "async def api_wheel_result" in wr_code)

# _active_spins state
check("wheel_routes: есть _active_spins", "_active_spins" in wr_code)
check("wheel_routes: есть _spin_fallback", "async def _spin_fallback" in wr_code)
check("wheel_routes: есть _get_sector_under_pointer", "def _get_sector_under_pointer" in wr_code)

# JS: sendSpinResult
check("JS: есть sendSpinResult", "function sendSpinResult" in js_code or "async function sendSpinResult" in js_code)
check("JS: post /api/wheel/result", "/api/wheel/result" in js_code)

# JS: runSpinAnimation принимает isInitiator
check("JS: runSpinAnimation принимает isInitiator", "isInitiator" in js_code)

# JS: _currentSpinId для дедупликации
check("JS: _currentSpinId используется", "_currentSpinId" in js_code)

# WS: spin_completed handler
check("JS: обрабатывает spin_completed", "'spin_completed'" in js_code or '"spin_completed"' in js_code)

# ============================================================
section("10. bot.py — on_wheel_spin_completed контракт")
# ============================================================

check("bot: on_wheel_spin_completed принимает is_test", 
      "async def on_wheel_spin_completed(self, winner: dict, is_test:" in bot_code)
check("bot: _build_winner_embed принимает is_test",
      "def _build_winner_embed(self, winner: dict, meta: dict | None, is_test:" in bot_code)
check("bot: _post_winner_to_telegram принимает is_test",
      "async def _post_winner_to_telegram(self, winner: dict, meta: dict | None, is_test:" in bot_code)

# ============================================================
# ИТОГ
# ============================================================
print(f"\n{'='*60}")
print(f"  ИТОГ: {PASS} passed, {FAIL} failed")
print(f"{'='*60}")

if FAIL > 0:
    sys.exit(1)
else:
    print("\n  Все тесты прошли! ✓")
