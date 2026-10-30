import pytest

from servers.filesystem.server import apply_unified_diff, safe_path


def test_apply_unified_diff_replaces_and_adds_lines() -> None:
    original = "alpha\nbeta\ngamma\n"
    patch = """--- a/demo.txt
+++ b/demo.txt
@@ -1,3 +1,4 @@
 alpha
-beta
+changed
 gamma
+delta
"""
    assert apply_unified_diff(original, patch) == "alpha\nchanged\ngamma\ndelta\n"


def test_safe_path_rejects_parent_escape() -> None:
    with pytest.raises(ValueError, match="escapes"):
        safe_path("../outside.txt")


def test_safe_path_rejects_absolute_path() -> None:
    with pytest.raises(ValueError, match="Absolute"):
        safe_path("/etc/passwd")
