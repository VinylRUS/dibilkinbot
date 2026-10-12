"""Тест функции определения сектора под указателем.
Проверяет совпадение между frontend (JS) и backend (Python).
"""
import math
import sys
sys.path.insert(0, '/home/z/my-project/kinovecher')

# Backend функция (копия из web.py)
def _get_sector_under_pointer(rotation: float, items_count: int) -> int:
    if items_count == 0:
        return -1
    angle_per = (2 * math.pi) / items_count
    norm = (-math.pi / 2 - rotation) % (2 * math.pi)
    if norm < 0:
        norm += 2 * math.pi
    return int(norm // angle_per) % items_count

# Frontend функция (копия из wheel.html)
def get_sector_under_pointer_js(rotation, itemsCount):
    if itemsCount == 0:
        return -1
    anglePer = (2 * math.pi) / itemsCount
    normalized = (-math.pi / 2 - rotation) % (2 * math.pi)
    # JS: % может вернуть отрицательное, Python: всегда положительное
    # В JS: if (normalized < 0) normalized += Math.PI * 2
    if normalized < 0:
        normalized += 2 * math.pi
    return math.floor(normalized / anglePer) % itemsCount

# Симулируем сценарии
print("=== Тест: совпадение Python vs JS ===")
test_cases = [
    # (rotation, items_count, description)
    (0, 4, "rotation=0, 4 items"),
    (math.pi / 2, 4, "rotation=π/2, 4 items"),
    (-math.pi / 2, 4, "rotation=-π/2, 4 items"),
    (math.pi, 4, "rotation=π, 4 items"),
    (5 * 2 * math.pi, 4, "5 оборотов, 4 items"),
    (5 * 2 * math.pi + math.pi / 2, 4, "5.25 оборотов, 4 items"),
    (35.7, 12, "большое rotation, 12 items"),
    (-100.5, 6, "отрицательное rotation, 6 items"),
    (0.001, 2, "почти 0, 2 items"),
    (math.pi - 0.001, 2, "почти π, 2 items (граница сектора)"),
]

all_match = True
for rotation, count, desc in test_cases:
    py = _get_sector_under_pointer(rotation, count)
    js = get_sector_under_pointer_js(rotation, count)
    match = "OK" if py == js else "MISMATCH!"
    if py != js:
        all_match = False
    print(f"  {desc:50s}  py={py}  js={js}  {match}")

print()
print("=== Тест: визуальное определение сектора ===")
# Симулируем сектора и где должен быть указатель
# В canvas: сектор 0 = углы [0, anglePer], сектор 1 = [anglePer, 2*anglePer]...
# Указатель вверху = -π/2
# После поворота rotation, сектор i на углах [i*anglePer+rotation, (i+1)*anglePer+rotation]
# Указатель указывает на сектор если i*anglePer+rotation <= -π/2 < (i+1)*anglePer+rotation
# → i = floor((-π/2 - rotation) / anglePer)

count = 4
angle_per = 2 * math.pi / count
print(f"  {count} секторов по {math.degrees(angle_per):.0f}° каждый")
print(f"  Сектор 0: углы [0°, {math.degrees(angle_per):.0f}°]")
print(f"  Сектор 1: углы [{math.degrees(angle_per):.0f}°, {math.degrees(2*angle_per):.0f}°]")
print(f"  Сектор 2: углы [{math.degrees(2*angle_per):.0f}°, {math.degrees(3*angle_per):.0f}°]")
print(f"  Сектор 3: углы [{math.degrees(3*angle_per):.0f}°, 360°]")
print()

# Симулируем rotation после 5 оборотов (5*2π = 31.42)
# Допустим хотим чтобы указатель был в центре сектора 2 (центр = 2*angle_per + angle_per/2 = 5*angle_per/2)
# Центр сектора 2 = 2 * (π/2) + π/4 = 5π/4
# rotation должен быть такой чтобы (5π/4 + rotation) = -π/2 (mod 2π)
# → rotation = -π/2 - 5π/4 = -7π/4 = π/4 (mod 2π) = π/4 + 5*2π = 31.42 + 0.785 = 32.2
target_sector = 2
center_angle = target_sector * angle_per + angle_per / 2
# Хотим чтобы center_angle + rotation ≡ -π/2 (mod 2π)
# rotation = -π/2 - center_angle (mod 2π)
desired_rotation = -math.pi / 2 - center_angle
# Добавим 5 оборотов
final_rotation = desired_rotation + 5 * 2 * math.pi

result = _get_sector_under_pointer(final_rotation, count)
print(f"  Хотели сектор {target_sector}, rotation={final_rotation:.4f}")
print(f"  Backend вернул: {result}")
print(f"  Ожидаемо: {target_sector == result}")
print()

# Симулируем что frontend отправляет с slightly другим rotation (из-за float)
result_off = _get_sector_under_pointer(final_rotation + 0.1, count)
print(f"  С возмущением +0.1: backend={result_off} (ожидался {target_sector})")

print()
print("=== Тест: симуляция реального спина ===")
# Симулируем физику: 5-7 оборотов + случайный offset
import random
random.seed(42)
for trial in range(5):
    extra_turns = 5 + random.randint(0, 2)
    random_offset = random.random() * 2 * math.pi
    total_rotation = extra_turns * 2 * math.pi + random_offset
    count = random.choice([4, 6, 8, 12, 2, 3])
    py_idx = _get_sector_under_pointer(total_rotation, count)
    js_idx = get_sector_under_pointer_js(total_rotation, count)
    print(f"  Trial {trial+1}: rotation={total_rotation:.3f}, items={count}, "
          f"py_sector={py_idx}, js_sector={js_idx}, match={py_idx == js_idx}")

print()
if all_match:
    print("=== ВСЕ ТЕСТЫ ПРОШЛИ ===")
else:
    print("=== ЕСТЬ РАСХОЖДЕНИЯ ===")
