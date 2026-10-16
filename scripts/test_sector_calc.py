"""Тест get_sector_under_pointer — определение сектора под указателем.

Проверяет:
1. Базовые случаи (0/1 items, rotation=0, осевые углы)
2. Центры секторов (rotation, ставящий i-й сектор под указатель)
3. Границы секторов (точки по разные стороны дают разные результаты)
4. Большие и отрицательные rotation
5. Случайные rotation → всегда валидный сектор
6. Периодичность mod 2π

В v2.4.0 функция живёт в wheel_physics.py (раньше была копия в web.py).
"""
import math
import os
import random
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from wheel_physics import get_sector_under_pointer

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        print(f"  \u2717 {name} \u2014 {detail}")


def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


# ============================================================
section("1. Базовые случаи")
# ============================================================

check("0 items → -1", get_sector_under_pointer(0.0, 0) == -1)
check("0 items, любой rotation → -1",
      get_sector_under_pointer(123.456, 0) == -1)
check("1 item → всегда 0",
      get_sector_under_pointer(0.0, 1) == 0)
check("1 item, любой rotation → 0",
      get_sector_under_pointer(999.999, 1) == 0)

# rotation=0, 4 items: norm = -π/2 mod 2π = 3π/2,
# idx = floor((3π/2)/(π/2)) = 3
check("rotation=0, 4 items → сектор 3",
      get_sector_under_pointer(0.0, 4) == 3)

# rotation=π/2: norm = -π mod 2π = π, idx = 2
check("rotation=π/2, 4 items → сектор 2",
      get_sector_under_pointer(math.pi / 2, 4) == 2)

# rotation=-π/2: norm = 0, idx = 0
check("rotation=-π/2, 4 items → сектор 0",
      get_sector_under_pointer(-math.pi / 2, 4) == 0)

# rotation=π: norm = π/2, idx = 1
check("rotation=π, 4 items → сектор 1",
      get_sector_under_pointer(math.pi, 4) == 1)


# ============================================================
section("2. Центры секторов")
# ============================================================

# Для items_count=N, центр сектора i = i*angle_per + angle_per/2.
# Чтобы центр был под указателем: center + rotation ≡ -π/2 (mod 2π)
# → rotation = -π/2 - center
for count in [2, 3, 4, 6, 8, 12]:
    angle_per = 2 * math.pi / count
    for i in range(count):
        center = i * angle_per + angle_per / 2
        rot = -math.pi / 2 - center
        result = get_sector_under_pointer(rot, count)
        check(f"count={count}, центр сектора {i} → {result}",
              result == i, f"ожидался {i}, получен {result}")


# ============================================================
section("3. Границы секторов — соседние точки разные")
# ============================================================

# Точную границу не проверяем (на ней возможны float-расхождения),
# но точки по разные стороны от границы должны давать РАЗНЫЕ сектора —
# это устойчивый инвариант.
for count in [4, 6, 8]:
    angle_per = 2 * math.pi / count
    eps = angle_per * 0.01
    all_different = True
    for i in range(count):
        # Граница между сектором i и i+1: rotation = -π/2 - (i+1)*angle_per
        boundary = -math.pi / 2 - (i + 1) * angle_per
        # Точка "внутри сектора i" (чуть правее границы)
        r_in_i = get_sector_under_pointer(boundary + eps, count)
        # Точка "внутри сектора i+1" (чуть левее границы)
        r_in_next = get_sector_under_pointer(boundary - eps, count)
        if r_in_i == r_in_next:
            all_different = False
            break
    check(f"count={count}: точки по разные стороны границы дают разные сектора",
          all_different)


# ============================================================
section("4. Большие и отрицательные rotation")
# ============================================================

# ВАЖНО: в float-арифметике N * 2π ≠ точно N*2π.
# На production rotation всегда случайный, поэтому edge cases с N*2π
# проверяем только на валидность диапазона, без точного expected.

check("100 оборотов, 4 items → валидный",
      0 <= get_sector_under_pointer(100 * 2 * math.pi, 4) < 4)

check("отрицательный rotation, 6 items → валидный",
      0 <= get_sector_under_pointer(-999.999, 6) < 6)

check("rotation=0.001, 2 items → валидный",
      0 <= get_sector_under_pointer(0.001, 2) < 2)

check("1000 оборотов, 8 items → валидный",
      0 <= get_sector_under_pointer(1000 * 2 * math.pi, 8) < 8)


# ============================================================
section("5. Случайные rotation — всегда валидный сектор")
# ============================================================

random.seed(42)
all_valid = True
for _ in range(1000):
    rot = random.uniform(-10000, 10000)
    count = random.randint(2, 50)
    result = get_sector_under_pointer(rot, count)
    if not (0 <= result < count):
        all_valid = False
        break
check("1000 случайных rotation (2..50 items) → все валидны", all_valid)


# ============================================================
section("6. Периодичность mod 2π")
# ============================================================

# Один полный оборот (2π) должен давать тот же результат, что rotation=0.
# В float `2 * math.pi` остаётся достаточно точным (отличие от 10π меньше
# эпсилона), поэтому один оборот — clean инвариант (5+ оборотов уже нет).
random.seed(7)
periodic_ok = True
for _ in range(100):
    rot = random.uniform(-100, 100)
    count = random.randint(2, 20)
    base = get_sector_under_pointer(rot, count)
    after_one_turn = get_sector_under_pointer(rot + 2 * math.pi, count)
    if after_one_turn != base:
        periodic_ok = False
        break
check("rot + 2π даёт тот же сектор (один оборот)", periodic_ok)


# ============================================================
# ИТОГ
# ============================================================
print(f"\n{'='*60}")
print(f"  ИТОГ: {PASS} passed, {FAIL} failed")
print(f"{'='*60}")
if FAIL > 0:
    sys.exit(1)
else:
    print("\n  Все тесты прошли! \u2713")
