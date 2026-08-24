"""temp_files 临时文件机制测试（无 AstrBot 依赖）。"""

from __future__ import annotations

import json
import os
import time

import pytest

from temp_files import (
    TEMP_NAME_RE,
    cleanup_temp,
    resolve_temp_file,
    write_temp_series,
)


def test_write_and_resolve_roundtrip(tmp_path):
    """写明细文件 → 安全解析 → 内容正确。"""
    series = [{"t": 1700000000 + i * 60, "hr": 70 + i} for i in range(5)]
    name = write_temp_series(tmp_path, series)
    assert TEMP_NAME_RE.match(name)

    target = resolve_temp_file(tmp_path, name)
    assert target is not None and target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data) == 5
    assert data[0]["hr"] == 70


def test_resolve_rejects_path_traversal(tmp_path):
    """路径穿越 / 非法文件名被拒绝。"""
    for bad in [
        "../../etc/passwd",
        "hr_20260824_235000_1a2b3c",  # 缺 .json
        "sub/hr_20260824_235000_1a2b3c.json",  # 带目录
        "hr_20260824_235000_zzzzzz.json",  # 非 hex
        "",
    ]:
        assert resolve_temp_file(tmp_path, bad) is None


def test_cleanup_temp_removes_old_files(tmp_path):
    """过期临时文件被清理，新文件保留。"""
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(parents=True)
    old = temp_dir / "hr_20260824_235000_1a2b3c.json"
    old.write_text("[]")
    new = temp_dir / "hr_20260824_235100_4d5e6f.json"
    new.write_text("[]")
    ts = time.time()
    os.utime(old, (ts - 7200, ts - 7200))
    cleanup_temp(temp_dir, max_age=3600)
    assert not old.exists()
    assert new.exists()


def test_resolve_missing_file_returns_path(tmp_path):
    """不存在的文件：resolve 仍返回路径（存在性由调用方判断）。"""
    target = resolve_temp_file(tmp_path, "hr_20260824_235000_1a2b3c.json")
    assert target is not None
    assert not target.exists()
