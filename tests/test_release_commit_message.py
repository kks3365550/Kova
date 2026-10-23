# -*- coding: utf-8 -*-
"""tools/release.py 커밋 메시지에 'release:' 접두어가 중복되지 않는지 확인한다."""
import importlib.util
import os

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "kova_release_msg", os.path.join(BASE_DIR, "tools", "release.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _commit_msg(monkeypatch, message):
    rel = _load()
    calls = []
    monkeypatch.setattr(rel.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    rel.run_git_release("9.9.9", message)
    commits = [c for c in calls if c[:2] == ["git", "commit"]]
    return commits[0][3]


@pytest.mark.parametrize("message,expected", [
    ("fix bug", "release: v9.9.9 - fix bug"),
    ("release: v9.9.9 - fix bug", "release: v9.9.9 - fix bug"),
    ("Release: v9.9.9: fix bug", "release: v9.9.9 - fix bug"),
    ("", "release: v9.9.9"),
    (None, "release: v9.9.9"),
    ("release: v9.9.9", "release: v9.9.9"),
])
def test_commit_message_prefix_not_duplicated(monkeypatch, message, expected):
    assert _commit_msg(monkeypatch, message) == expected
