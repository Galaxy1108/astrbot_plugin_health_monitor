"""健康数据临时文件（供 LLM 读取明细，读取后自动销毁）。

无第三方依赖，方便独立测试。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

#: 临时文件自动清理：超过该秒数的文件在下一次工具调用时删除
TEMP_MAX_AGE_SECONDS = 3600

#: 临时文件名的安全模式（读取工具只允许这类文件）
TEMP_NAME_RE = re.compile(r"^hr_\d{8}_\d{6}_[0-9a-f]{6}\.json$")


def write_temp_series(data_dir: Path, series: list[dict]) -> str:
    """把明细序列写入 data_dir/temp/，返回文件名（相对 temp 目录）。"""
    temp_dir = data_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    cleanup_temp(temp_dir)
    name = f"hr_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}.json"
    (temp_dir / name).write_text(
        json.dumps(series, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return name


def cleanup_temp(temp_dir: Path, max_age: float | None = None) -> None:
    """删除过期临时文件（自动销毁兜底）。"""
    if max_age is None:
        max_age = TEMP_MAX_AGE_SECONDS
    try:
        now = time.time()
        for f in temp_dir.glob("*.json"):
            try:
                if now - f.stat().st_mtime > max_age:
                    f.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def resolve_temp_file(data_dir: Path, raw_name: str) -> Path | None:
    """把用户给定的文件名安全解析为 temp 目录内的文件；非法返回 None。"""
    if "/" in raw_name or "\\" in raw_name:
        return None
    temp_dir = data_dir / "temp"
    name = Path(raw_name).name  # 去掉任何路径成分，防穿越
    if not TEMP_NAME_RE.match(name):
        return None
    target = (temp_dir / name).resolve()
    if not str(target).startswith(str(temp_dir.resolve())):
        return None
    return target
