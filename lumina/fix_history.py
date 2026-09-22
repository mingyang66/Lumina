import os

base = r'D:\workplace-python\Lumina\lumina'
path = os.path.join(base, 'history_panel.py')
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find PinWindow class and truncate
pin_idx = None
for i, line in enumerate(lines):
    if line.strip() == 'class PinWindow:':
        pin_idx = i
        break

if pin_idx is not None:
    # Keep everything before PinWindow (remove the preceding blank lines too)
    # Remove from pin_idx-2 (blank lines before) or pin_idx-1
    new_lines = lines[:pin_idx]
    # Remove trailing blank lines
    while new_lines and new_lines[-1].strip() == '':
        new_lines.pop()
    new_lines.append('\n')
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print(f"Removed PinWindow from history_panel.py. New size: {len(new_lines)} lines")
else:
    print("PinWindow not found in history_panel.py")
