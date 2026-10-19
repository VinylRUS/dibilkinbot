"""Удаляет старые wheel+movienight роуты из web.py.
Сохраняет backup в web.py.bak."""
import re

with open('/home/z/my-project/kinovecher/web.py', 'r') as f:
    lines = f.readlines()

# Find line ranges to remove (0-indexed)
# We'll mark lines for deletion
to_delete = set()

# 1. Old _get_git_commit_id and _get_bot_version (they're in deps.py now)
i = 0
while i < len(lines):
    line = lines[i]
    # Remove old function definitions that are now in deps.py
    if line.startswith('def _get_git_commit_id('):
        # Find end of function (next line at same indent level or empty)
        while i < len(lines) and (lines[i].startswith('def _get_git_commit_id') or
                                   lines[i][0:4] == '    ' or lines[i].strip() == ''):
            to_delete.add(i)
            i += 1
            if i < len(lines) and (lines[i].startswith('def ') or lines[i].startswith('class ') or
                                   lines[i].startswith('#') or lines[i].startswith('@app')):
                break
        continue
    if line.startswith('def _get_bot_version('):
        while i < len(lines) and (lines[i].startswith('def _get_bot_version') or
                                   lines[i][0:4] == '    ' or lines[i].strip() == ''):
            to_delete.add(i)
            i += 1
            if i < len(lines) and (lines[i].startswith('def ') or lines[i].startswith('class ') or
                                   lines[i].startswith('#') or lines[i].startswith('@app') or
                                   lines[i].startswith('templates')):
                break
        continue
    # Remove duplicate templates.env.globals lines (keep the ones at top)
    if i > 130 and 'templates.env.globals' in line and i not in to_delete:
        # Only remove if it's a duplicate (after line 130)
        # Keep the ones at lines 50-52 which we already set
        if "bot_version" in line or "commit_id" in line or "bot_version_with_commit" in line:
            to_delete.add(i)
            i += 1
            continue

    i += 1

# 2. Remove old auth functions (get_current_user, require_user, require_admin, require_superuser)
#    and helpers (get_current_guild_id, _anonymize_items)
i = 0
while i < len(lines):
    line = lines[i]
    if any(line.startswith(f'async def {fn}(') or line.startswith(f'def {fn}(') for fn in
           ['get_current_user', 'require_user', 'require_admin', 'require_superuser',
            'get_current_guild_id', '_anonymize_items']):
        # Remove function and its body
        while i < len(lines):
            to_delete.add(i)
            i += 1
            if i >= len(lines):
                break
            # Stop at next function/class/decorator at module level
            if (lines[i] and not lines[i][0].isspace() and
                lines[i].strip() != '' and
                not lines[i].startswith('#')):
                break
        continue
    i += 1

# 3. Remove _wheel_spin_locks and _active_spins
i = 0
while i < len(lines):
    line = lines[i]
    if '_wheel_spin_locks' in line and 'dict' in line and line.startswith('_wheel_spin_locks'):
        to_delete.add(i)
        i += 1
        continue
    if '_active_spins' in line and 'dict' in line and line.startswith('_active_spins'):
        to_delete.add(i)
        i += 1
        continue
    i += 1

# 4. Remove all @app routes related to wheel/movienight/collection/WS
# Find start of wheel section (GET /wheel) and end of movienight section
wheel_start = None
movienight_end = None
ws_wheel_start = None
ws_wheel_end = None

for i, line in enumerate(lines):
    if '@app.get("/wheel"' in line and wheel_start is None:
        wheel_start = i
    # End of movienight: find the next @app route after collection that's NOT wheel/movienight
    if wheel_start is not None and movienight_end is None:
        if (line.startswith('@app.') and 
            '/wheel' not in line and '/collection' not in line and
            '/movienight' not in line and '/collect/' not in line and
            '/api/wheel' not in line and '/filmnight' not in line):
            movienight_end = i
            break

# Also find WebSocket /ws/wheel
for i, line in enumerate(lines):
    if '@app.websocket("/ws/wheel")' in line:
        ws_wheel_start = i
    if ws_wheel_start is not None and i > ws_wheel_start + 2:
        if line.startswith('@app.') or line.startswith('# ===') or line.startswith('async def _retro'):
            ws_wheel_end = i
            break

print(f"Wheel section: lines {wheel_start+1}-{movienight_end}" if wheel_start else "Wheel section: NOT FOUND")
print(f"WS section: lines {ws_wheel_start+1}-{ws_wheel_end}" if ws_wheel_start else "WS section: NOT FOUND")

# Mark wheel+movienight section for deletion
if wheel_start and movienight_end:
    for i in range(wheel_start, movienight_end):
        to_delete.add(i)

# Mark WS section for deletion
if ws_wheel_start and ws_wheel_end:
    for i in range(ws_wheel_start, ws_wheel_end):
        to_delete.add(i)

# Write result
new_lines = [line for i, line in enumerate(lines) if i not in to_delete]

# Write backup
with open('/home/z/my-project/kinovecher/web.py.bak', 'w') as f:
    f.writelines(lines)

with open('/home/z/my-project/kinovecher/web.py', 'w') as f:
    f.writelines(new_lines)

print(f"Removed {len(to_delete)} lines")
print(f"Old size: {len(lines)} lines")
print(f"New size: {len(new_lines)} lines")
