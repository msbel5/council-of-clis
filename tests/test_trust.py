"""Unit tests for the project-folder trust model."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import trust
from trust import (
    TrustError,
    canonicalize,
    check,
    is_forbidden,
    trust_folder,
    untrust_folder,
)


@pytest.fixture(autouse=True)
def isolated_trust_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect TRUST_STORE to a temp file per-test so we never touch real config."""
    store = tmp_path / "trusted_folders.json"
    monkeypatch.setattr(trust, "TRUST_STORE", store)
    monkeypatch.setattr(trust, "USER_CONFIG_DIR", tmp_path)
    return store


def test_canonicalize_existing_dir(tmp_path: Path) -> None:
    assert canonicalize(tmp_path) == tmp_path.resolve()


def test_canonicalize_user_expansion(tmp_path: Path) -> None:
    # ~ expansion works (this just verifies the call doesn't break on tilde-style input)
    assert canonicalize(str(tmp_path)) == tmp_path.resolve()


def test_canonicalize_missing_raises() -> None:
    with pytest.raises(TrustError):
        canonicalize("/this/path/does/not/exist/anywhere/ever")


def test_canonicalize_file_raises(tmp_path: Path) -> None:
    f = tmp_path / "afile.txt"
    f.write_text("x")
    with pytest.raises(TrustError, match="not a directory"):
        canonicalize(f)


def test_forbidden_root_blocked_on_posix() -> None:
    if sys.platform.startswith("win"):
        pytest.skip("posix-only check")
    assert is_forbidden(Path("/etc")) is not None
    assert is_forbidden(Path("/etc/passwd").parent) is not None


def test_forbidden_root_blocked_on_windows() -> None:
    if not sys.platform.startswith("win"):
        pytest.skip("windows-only check")
    assert is_forbidden(Path("C:/Windows")) is not None
    assert is_forbidden(Path("C:/Windows/System32")) is not None


def test_safe_path_not_forbidden(tmp_path: Path) -> None:
    assert is_forbidden(tmp_path) is None


def test_check_empty_returns_trusted() -> None:
    decision = check("")
    assert decision.is_trusted
    assert decision.reason == "trusted"


def test_check_unknown_dir_needs_approval(tmp_path: Path) -> None:
    decision = check(tmp_path)
    assert not decision.is_trusted
    assert decision.reason == "needs-approval"
    assert decision.canonical == tmp_path.resolve()


def test_check_trusted_after_approval(tmp_path: Path) -> None:
    trust_folder(tmp_path.resolve(), note="test")
    decision = check(tmp_path)
    assert decision.is_trusted
    assert decision.reason == "trusted"


def test_check_forbidden_path_refused() -> None:
    if sys.platform.startswith("win"):
        decision = check("C:/Windows")
    else:
        decision = check("/etc")
    assert not decision.is_trusted
    assert decision.reason.startswith("forbidden:")


def test_trust_forbidden_path_raises() -> None:
    target = Path("C:/Windows") if sys.platform.startswith("win") else Path("/etc")
    with pytest.raises(TrustError, match="forbidden"):
        trust_folder(target)


def test_untrust_removes_path(tmp_path: Path) -> None:
    canonical = tmp_path.resolve()
    trust_folder(canonical, note="test")
    assert check(tmp_path).is_trusted
    assert untrust_folder(canonical) is True
    assert not check(tmp_path).is_trusted


def test_untrust_unknown_returns_false(tmp_path: Path) -> None:
    assert untrust_folder(tmp_path.resolve()) is False


def test_check_returns_reason_on_missing_path() -> None:
    decision = check("/path/that/does/not/exist/xyz123")
    assert not decision.is_trusted
    assert decision.reason.startswith("not-a-directory")
