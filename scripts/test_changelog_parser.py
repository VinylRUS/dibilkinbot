"""Тест парсера CHANGELOG.md — проверяет что новый формат с тегами парсится корректно."""
import sys
sys.path.insert(0, '/home/z/my-project/kinovecher')

from changelog_parser import parse_changelog, get_latest_version, get_latest_changelog

print("=== Тест: парсинг CHANGELOG.md ===\n")

entries = parse_changelog("/home/z/my-project/kinovecher/CHANGELOG.md")
print(f"Всего распарсено версий: {len(entries)}")
print()

print("=== Первые 3 версии ===")
for e in entries[:3]:
    print(f"\n• {e.version} (hash={e.commit_hash})")
    print(f"  Секции: {list(e.sections.keys())}")
    print(f"  public_sections: {list(e.public_sections.keys())}")
    print(f"  admin_sections: {list(e.admin_sections.keys())}")

print("\n\n=== Последняя версия (v2.3.2) — содержимое секций ===")
latest = get_latest_changelog("/home/z/my-project/kinovecher/CHANGELOG.md")
if latest:
    print(f"Version: {latest.version} (hash={latest.commit_hash})")
    for sec, items in latest.sections.items():
        print(f"\n[{sec}] ({len(items)} items)")
        for it in items[:3]:
            print(f"  - {it[:100]}{'...' if len(it) > 100 else ''}")
        if len(items) > 3:
            print(f"  ... и ещё {len(items)-3} пунктов")

print("\n\n=== Что пойдёт в общий канал (public_sections) ===")
if latest:
    for sec, items in latest.public_sections.items():
        print(f"  [{sec}]: {len(items)} items")

print("\n=== Что пойдёт админу в DM (admin_sections) ===")
if latest:
    for sec, items in latest.admin_sections.items():
        print(f"  [{sec}]: {len(items)} items")
        for it in items:
            print(f"    • {it[:80]}")
