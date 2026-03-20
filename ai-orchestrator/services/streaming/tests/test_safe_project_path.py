"""
tests/test_safe_project_path.py
================================
Tests for safe_project_path() — Phase 1.5 path-traversal protection.
"""
import pytest
from pathlib import Path
from services.streaming.core.security_config import safe_project_path


@pytest.fixture()
def base(tmp_path: Path) -> str:
    return str(tmp_path)


def test_valid_relative_path(base):
    result = safe_project_path("myproject/src", base_dir=base)
    assert result == str(Path(base) / "myproject" / "src")


def test_valid_nested_path(base):
    result = safe_project_path("a/b/c/d", base_dir=base)
    assert result.startswith(base)


def test_dot_dot_traversal_rejected(base):
    with pytest.raises(ValueError, match="Path traversal"):
        safe_project_path("../../etc/passwd", base_dir=base)


def test_single_dot_dot_rejected(base):
    with pytest.raises(ValueError, match="Path traversal"):
        safe_project_path("../sibling", base_dir=base)


def test_absolute_path_outside_base_rejected(base, tmp_path):
    outside = str(tmp_path.parent / "outside")
    with pytest.raises(ValueError):
        safe_project_path(outside, base_dir=base)


def test_empty_path_resolves_to_base(base):
    result = safe_project_path("", base_dir=base)
    assert result == str(Path(base).resolve())


def test_path_with_encoded_traversal(base):
    # URL-decoded form — should still be caught by Path.resolve()
    with pytest.raises(ValueError):
        safe_project_path("..%2F..%2Fetc%2Fpasswd", base_dir=base)
