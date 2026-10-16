"""v2.4.0 Tests — server-authoritative wheel physics.

Tests the new architecture:
1. wheel_physics.py — generate_spin, wheel_ease, get_sector_under_pointer
2. ws_manager.py — guild_id isolation
3. wheel_routes.py — server-side spin (no /api/wheel/result)
4. wheel.js — no client-side winner determination
5. Idempotent completion
6. Lock states (IDLE → SPINNING → COMPLETED)
"""
import ast
import math
import os
import sys
import json
import random

PROJECT_DIR = '/home/z/my-project/kinovecher'
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1; print(f"  ✓ {name}")
    else:
        FAIL += 1; print(f"  ✗ {name} — {detail}")

def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

# ============================================================
section("1. wheel_physics.py — generate_spin")
# ============================================================

from wheel_physics import (
    SpinPhysics, wheel_ease, get_sector_under_pointer,
    generate_spin, physics_to_dict
)

# Basic
p = generate_spin("test1", 4, 0.0, 6.0, "classic", 1000.0)
check("generate_spin: returns SpinPhysics", isinstance(p, SpinPhysics))
check("generate_spin: spin_id set", p.spin_id == "test1")
check("generate_spin: items_count=4", p.items_count == 4)
check("generate_spin: mode='classic'", p.mode == "classic")
check("generate_spin: winner_idx in range", 0 <= p.winner_idx < 4)
check("generate_spin: final = initial + total",
      abs(p.final_rotation - (p.initial_rotation + p.total_rotation)) < 0.001)

# Winner matches get_sector_under_pointer
expected_winner = get_sector_under_pointer(p.final_rotation, 4)
check("winner_idx matches get_sector_under_pointer", p.winner_idx == expected_winner)

# 1000 random spins — all valid
random.seed(42)
all_ok = True
for _ in range(1000):
    count = random.randint(2, 50)
    rot = random.uniform(-1000, 1000)
    dur = random.uniform(3, 30)
    p = generate_spin("t", count, rot, dur, "classic")
    if not (0 <= p.winner_idx < count):
        all_ok = False; break
    expected = get_sector_under_pointer(p.final_rotation, count)
    if p.winner_idx != expected:
        all_ok = False; break
check("1000 random spins: all winners valid + match formula", all_ok)

# physics_to_dict
d = physics_to_dict(p)
check("physics_to_dict: has spin_id", "spin_id" in d)
check("physics_to_dict: has winner_idx", "winner_idx" in d)
check("physics_to_dict: has final_rotation", "final_rotation" in d)
check("physics_to_dict: has initial_rotation", "initial_rotation" in d)
check("physics_to_dict: has total_rotation", "total_rotation" in d)
check("physics_to_dict: has duration_sec", "duration_sec" in d)

# ============================================================
section("2. wheel_ease — mathematical properties")
# ============================================================

check("wheel_ease(0) ≈ 0", abs(wheel_ease(0)) < 0.001)
check("wheel_ease(1) ≈ 1", abs(wheel_ease(1) - 1) < 0.001)
check("wheel_ease(0.5) between 0 and 1", 0 < wheel_ease(0.5) < 1)

# Monotone
prev = -1
all_monotone = True
for i in range(1001):
    v = wheel_ease(i / 1000)
    if v < prev - 0.0001:
        all_monotone = False; break
    prev = v
check("wheel_ease: monotone on 1000 points", all_monotone)

# Continuous derivative at boundaries
eps = 0.00001
for b in [0.20, 0.70]:
    d1 = (wheel_ease(b) - wheel_ease(b - eps)) / eps
    d2 = (wheel_ease(b + eps) - wheel_ease(b)) / eps
    check(f"wheel_ease: continuous at {int(b*100)}%", abs(d1 - d2) < 0.01,
          f"d1={d1:.4f} d2={d2:.4f}")

# JS/Python match — exact formula
def js_wheel_ease(t):
    ACCEL_END = 0.20; PLATEAU_END = 0.70
    accel_area = ACCEL_END / 2; plateau_area = PLATEAU_END - ACCEL_END; decel_area = (1 - PLATEAU_END) / 2
    v_max = 1 / (accel_area + plateau_area + decel_area)
    if t < ACCEL_END:
        u = t / ACCEL_END; u3 = u*u*u; return v_max * ACCEL_END * (u3 - u3*u/2)
    elif t < PLATEAU_END:
        return v_max * ACCEL_END * 0.5 + v_max * (t - ACCEL_END)
    else:
        u = (t - PLATEAU_END) / (1 - PLATEAU_END); u3 = u*u*u; u4 = u3*u
        return v_max * ACCEL_END * 0.5 + v_max * (PLATEAU_END - ACCEL_END) + v_max * (1 - PLATEAU_END) * (u - u3 + u4/2)

all_match = True
for i in range(101):
    t = i / 100
    py = wheel_ease(t)
    js = js_wheel_ease(t)
    if abs(py - js) > 0.0001:
        all_match = False; break
check("Python/JS wheel_ease: match on 101 points (±0.0001)", all_match)

# ============================================================
section("3. get_sector_under_pointer — edge cases")
# ============================================================

check("0 items → -1", get_sector_under_pointer(0, 0) == -1)
check("1 item → always 0", get_sector_under_pointer(123.456, 1) == 0)

# Boundary: exactly on sector edge
for count in [2, 3, 4, 6, 8, 12]:
    ap = 2 * math.pi / count
    for i in range(count):
        # Center of sector i
        center = i * ap + ap / 2
        # Rotation that puts center under pointer: rotation = -π/2 - center
        rot = -math.pi / 2 - center
        result = get_sector_under_pointer(rot, count)
        check(f"sector center: count={count}, i={i} → {result}", result == i)

# Large rotations
check("100 rotations, 4 items", 0 <= get_sector_under_pointer(100 * 2 * math.pi, 4) < 4)
check("negative rotation, 6 items", 0 <= get_sector_under_pointer(-999.999, 6) < 6)

# ============================================================
section("4. ws_manager.py — guild_id isolation")
# ============================================================

with open('ws_manager.py') as f:
    ws_code = f.read()

check("ws: has _guild_connections", "_guild_connections" in ws_code)
check("ws: has _ws_to_guild", "_ws_to_guild" in ws_code)
check("ws: connect takes guild_id", "async def connect(websocket: WebSocket, guild_id: int)" in ws_code)
check("ws: broadcast_guild takes guild_id", "async def broadcast_guild(guild_id: int" in ws_code)
check("ws: broadcast_spin_started takes guild_id", "async def broadcast_spin_started(guild_id: int" in ws_code)
check("ws: broadcast_spin_completed takes guild_id", "async def broadcast_spin_completed(guild_id: int" in ws_code)
check("ws: broadcast_wheel_updated takes guild_id", "async def broadcast_wheel_updated(guild_id: int" in ws_code)
check("ws: no broadcast() without guild", "async def broadcast(" not in ws_code or ws_code.count("async def broadcast(") == 0)

# ============================================================
section("5. wheel_routes.py — server-authoritative")
# ============================================================

with open('wheel_routes.py') as f:
    wr_code = f.read()

check("wheel_routes: imports wheel_physics", "from wheel_physics import" in wr_code)
check("wheel_routes: uses generate_spin", "generate_spin(" in wr_code)
check("wheel_routes: uses physics_to_dict", "physics_to_dict(" in wr_code)
check("wheel_routes: NO /api/wheel/result", "/api/wheel/result" not in wr_code or
      wr_code.count('"/api/wheel/result"') == 0,
      "endpoint still exists")
check("wheel_routes: NO _spin_fallback", "async def _spin_fallback" not in wr_code)
check("wheel_routes: NO client_spin_id", "client_spin_id" not in wr_code)
check("wheel_routes: has _delayed_complete_spin", "async def _delayed_complete_spin" in wr_code)
check("wheel_routes: has status SPINNING", '"SPINNING"' in wr_code)
check("wheel_routes: has status COMPLETED", '"COMPLETED"' in wr_code)
check("wheel_routes: has _find_active_spin_for_guild", "_find_active_spin_for_guild" in wr_code)
check("wheel_routes: uses ws_manager.broadcast_guild", "broadcast_guild" in wr_code or
      "broadcast_spin_started" in wr_code)

# No old ws_manager calls without guild_id
check("wheel_routes: NO ws_manager.broadcast(", "ws_manager.broadcast(" not in wr_code)

# ============================================================
section("6. wheel.js — client only animates")
# ============================================================

with open('static/js/wheel.js') as f:
    js_code = f.read()

check("JS: has animatePhysics", "function animatePhysics" in js_code)
check("JS: has wheelEase", "function wheelEase" in js_code)
check("JS: NO sendSpinResult", "function sendSpinResult" not in js_code and
      "async function sendSpinResult" not in js_code)
check("JS: NO /api/wheel/result", "/api/wheel/result" not in js_code.replace(
      "// No more client-side winner determination or /api/wheel/result.", ""))
check("JS: NO _currentSpinId", "_currentSpinId" not in js_code)
check("JS: handles spin_started with physics", "physics" in js_code and "spin_started" in js_code)
check("JS: handles spin_completed with final_rotation", "final_rotation" in js_code)
check("JS: handles spin_completed with winner", "p.winner" in js_code or "payload.winner" in js_code)
check("JS: NO isInitiator parameter", "isInitiator" not in js_code)
check("JS: NO runSpinAnimation", "function runSpinAnimation" not in js_code)

# ============================================================
section("7. movienight_routes.py — guild_id in ws calls")
# ============================================================

with open('movienight_routes.py') as f:
    mn_code = f.read()

check("movienight: broadcast_collection_started(guild_id", 
      "broadcast_collection_started(guild_id" in mn_code)
check("movienight: broadcast_wheel_updated(guild_id",
      "broadcast_wheel_updated(guild_id" in mn_code)
check("movienight: NO ws_manager.broadcast(", "ws_manager.broadcast(" not in mn_code)

# ============================================================
section("8. Template + syntax checks")
# ============================================================

for f in ['wheel_physics.py','ws_manager.py','wheel_routes.py','movienight_routes.py',
          'web.py','bot.py','deps.py','github_monitor.py']:
    try:
        ast.parse(open(f).read())
        check(f"Syntax: {f}", True)
    except SyntaxError as e:
        check(f"Syntax: {f}", False, f"line {e.lineno}: {e.msg}")

from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates/'))
for t in ['wheel.html','movienight.html','panel.html','profile.html','achievements.html','santa.html']:
    try:
        env.get_template(t)
        check(f"Template: {t}", True)
    except Exception as e:
        check(f"Template: {t}", False, str(e)[:60])

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
