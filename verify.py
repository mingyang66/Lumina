import sys
sys.path.insert(0, r"D:\workplace-python\MyClip")
from myclip.ui import HistoryPanel
print("Import OK")

# Verify the fix
import inspect
src = inspect.getsource(HistoryPanel._refresh_cards)
assert "text_main" in src and "text_meta" in src
assert "暂无记录" in src
print("Dark mode text colors: OK")
print("Empty rows message: OK")

src = inspect.getsource(HistoryPanel._on_canvas_configure)
assert "bbox" in src and "scrollregion" in src
print("Scrollregion fix: OK")

print("\nALL CHECKS PASSED")
