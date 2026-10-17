"""Тест парсера CHANGELOG.md.

Проверяет:
1. parse_changelog — парсит существующий CHANGELOG.md, возвращает непустой список
2. Структура ChangelogEntry: version, commit_hash, sections
3. Новый формат с тегами (#НОВЫЕ_ФИЧИ# / #АДМИНСКИЕ_ФИЧИ# / #ТЕХНИЧЕСКАЯ_ИНФОРМАЦИЯ#)
4. public_sections исключает админские и технические
5. admin_sections содержит только админские
6. get_latest_version / get_latest_changelog
7. Парсинг synthetic-CHANGELOG в старом формате (### Заголовок)
8. Парсинг synthetic-CHANGELOG в новом формате с тегами
9. Пустой / несуществующий файл — возвращает []
"""
import os
import sys
import tempfile

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from changelog_parser import (
    parse_changelog,
    get_latest_version,
    get_latest_changelog,
    TAG_TO_KEY,
    NON_PUBLIC_SECTIONS,
    ADMIN_SECTIONS,
    ChangelogEntry,
)

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


CHANGELOG_PATH = os.path.join(PROJECT_DIR, "CHANGELOG.md")


# ============================================================
section("1. parse_changelog — реальный CHANGELOG.md")
# ============================================================

entries = parse_changelog(CHANGELOG_PATH)
check("parse_changelog возвращает list", isinstance(entries, list))
check("parse_changelog: список не пустой", len(entries) > 0,
      "получено 0 записей")
check("parse_changelog: элементы — ChangelogEntry",
      all(isinstance(e, ChangelogEntry) for e in entries))

if entries:
    first = entries[0]
    check(f"первая запись: версия {first.version!r}",
          first.version.startswith("v"),
          f"version={first.version!r}")
    check("первая запись: есть commit_hash",
          first.commit_hash is not None,
          "commit_hash=None")
    check("первая запись: hash выглядит как git hex",
          first.commit_hash is not None and len(first.commit_hash) == 7,
          f"hash={first.commit_hash!r}")
    check("первая запись: есть секции",
          len(first.sections) > 0,
          "sections пустой")


# ============================================================
section("2. Структура ChangelogEntry — новый формат с тегами")
# ============================================================

# В новом формате (v2.3.2+) первая запись должна иметь хотя бы одну из секций.
# Не все записи имеют все 3 секции — например, v2.4.1 имеет только Новое + Техническая.
if entries:
    first = entries[0]
    check("первая запись: есть хотя бы одна секция",
          len(first.sections) > 0,
          f"sections={list(first.sections.keys())}")
    check("первая запись: есть секция 'Новое' или 'Техническая информация'",
          "Новое" in first.sections or "Техническая информация" in first.sections)
    check("первая запись: есть секция 'Техническая информация'",
          "Техническая информация" in first.sections)

    # Секции должны быть lists of strings
    for sec_name, items in first.sections.items():
        check(f"секция '{sec_name}': list[str]",
              isinstance(items, list) and all(isinstance(i, str) for i in items))


# ============================================================
section("3. public_sections / admin_sections")
# ============================================================

if entries:
    first = entries[0]
    public = first.public_sections
    admin = first.admin_sections

    check("public: не содержит 'Админские фичи'",
          "Админские фичи" not in public)
    check("public: не содержит 'Техническая информация'",
          "Техническая информация" not in public)
    check("public: содержит 'Новое' (если есть в записи)",
          "Новое" not in first.sections or "Новое" in public)

    # admin содержит только админские секции (если они есть в записи)
    check("admin: содержит 'Админские фичи' (если есть в записи)",
          "Админские фичи" not in first.sections or "Админские фичи" in admin)
    check("admin: не содержит 'Новое'",
          "Новое" not in admin)
    check("admin: не содержит 'Техническая информация'",
          "Техническая информация" not in admin)


# ============================================================
section("4. get_latest_version / get_latest_changelog")
# ============================================================

latest_version = get_latest_version(CHANGELOG_PATH)
check(f"get_latest_version: {latest_version!r}",
      latest_version.startswith("v"),
      f"получено: {latest_version!r}")

check("get_latest_version == entries[0].version",
      latest_version == entries[0].version if entries else False)

latest_entry = get_latest_changelog(CHANGELOG_PATH)
check("get_latest_changelog: возвращает ChangelogEntry",
      isinstance(latest_entry, ChangelogEntry))
check("get_latest_changelog.version == get_latest_version",
      latest_entry is not None and latest_entry.version == latest_version)


# ============================================================
section("5. Парсинг synthetic-CHANGELOG в новом формате")
# ============================================================

new_format = """# Changelog

## v9.9.9 (abcdef1)

#НОВЫЕ_ФИЧИ#
- Новая фича 1
- Новая фича 2

#АДМИНСКИЕ_ФИЧИ#
- Секретная фича

#ТЕХНИЧЕСКАЯ_ИНФОРМАЦИЯ#
- Рефакторинг
"""

with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
    f.write(new_format)
    tmp_path = f.name

try:
    parsed = parse_changelog(tmp_path)
    check("новый формат: 1 запись", len(parsed) == 1, f"получено {len(parsed)}")
    if parsed:
        e = parsed[0]
        check("новый формат: version='v9.9.9'", e.version == "v9.9.9")
        check("новый формат: commit_hash='abcdef1'", e.commit_hash == "abcdef1")
        check("новый формат: 3 секции", len(e.sections) == 3,
              f"получено {len(e.sections)} ({list(e.sections.keys())})")
        check("новый формат: 'Новое' содержит 2 элемента",
              len(e.sections.get("Новое", [])) == 2)
        check("новый формат: 'Админские фичи' содержит 1 элемент",
              len(e.sections.get("Админские фичи", [])) == 1)
        check("новый формат: 'Техническая информация' содержит 1 элемент",
              len(e.sections.get("Техническая информация", [])) == 1)
        check("новый формат: 'Новое'[0] = 'Новая фича 1'",
              e.sections.get("Новое", [""])[0] == "Новая фича 1")
finally:
    os.unlink(tmp_path)


# ============================================================
section("6. Парсинг synthetic-CHANGELOG в старом формате")
# ============================================================

legacy_format = """# Changelog

## v1.0.0

### Новое
- Старая фича 1
- Старая фича 2

### Исправлено
- Багфикс
"""

with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
    f.write(legacy_format)
    tmp_path = f.name

try:
    parsed = parse_changelog(tmp_path)
    check("старый формат: 1 запись", len(parsed) == 1, f"получено {len(parsed)}")
    if parsed:
        e = parsed[0]
        check("старый формат: version='v1.0.0'", e.version == "v1.0.0")
        check("старый формат: commit_hash=None (нет хэша)",
              e.commit_hash is None)
        check("старый формат: 2 секции", len(e.sections) == 2,
              f"получено {len(e.sections)}")
        check("старый формат: есть 'Новое'", "Новое" in e.sections)
        check("старый формат: есть 'Исправлено'", "Исправлено" in e.sections)
        # В старом формате нет админских секций
        check("старый формат: public = все секции",
              set(e.public_sections.keys()) == set(e.sections.keys()))
        check("старый формат: admin = пусто",
              len(e.admin_sections) == 0)
finally:
    os.unlink(tmp_path)


# ============================================================
section("7. Edge cases — пустой / несуществующий файл")
# ============================================================

check("несуществующий файл → []",
      parse_changelog("/nonexistent/path/CHANGELOG.md") == [])

with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
    f.write("")
    tmp_path = f.name
try:
    check("пустой файл → []",
          parse_changelog(tmp_path) == [])
finally:
    os.unlink(tmp_path)

# Файл без заголовков версий
no_versions = "# Just a README\nNo versions here.\n"
with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
    f.write(no_versions)
    tmp_path = f.name
try:
    check("файл без '## vX.Y.Z' → []",
          parse_changelog(tmp_path) == [])
finally:
    os.unlink(tmp_path)


# ============================================================
section("8. Константы TAG_TO_KEY / NON_PUBLIC_SECTIONS / ADMIN_SECTIONS")
# ============================================================

check("TAG_TO_KEY: 3 тега",
      len(TAG_TO_KEY) == 3,
      f"получено {len(TAG_TO_KEY)}")
check("TAG_TO_KEY: #НОВЫЕ_ФИЧИ# → 'Новое'",
      TAG_TO_KEY.get("#НОВЫЕ_ФИЧИ#") == "Новое")
check("TAG_TO_KEY: #АДМИНСКИЕ_ФИЧИ# → 'Админские фичи'",
      TAG_TO_KEY.get("#АДМИНСКИЕ_ФИЧИ#") == "Админские фичи")
check("TAG_TO_KEY: #ТЕХНИЧЕСКАЯ_ИНФОРМАЦИЯ# → 'Техническая информация'",
      TAG_TO_KEY.get("#ТЕХНИЧЕСКАЯ_ИНФОРМАЦИЯ#") == "Техническая информация")

check("NON_PUBLIC_SECTIONS: 2 элемента",
      len(NON_PUBLIC_SECTIONS) == 2)
check("NON_PUBLIC_SECTIONS: содержит 'Админские фичи'",
      "Админские фичи" in NON_PUBLIC_SECTIONS)
check("NON_PUBLIC_SECTIONS: содержит 'Техническая информация'",
      "Техническая информация" in NON_PUBLIC_SECTIONS)

check("ADMIN_SECTIONS: 1 элемент",
      len(ADMIN_SECTIONS) == 1)
check("ADMIN_SECTIONS: содержит 'Админские фичи'",
      "Админские фичи" in ADMIN_SECTIONS)


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
