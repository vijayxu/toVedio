"""仓库与过程产物目录：默认 <repo>/artifacts/（tmp、staging、exports）。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def repo_root() -> Path:
    """toVedio 仓库根目录（含 demo/、根目录 run_tovedio.py）。"""
    # 本文件位于 src/tovedio/paths.py
    return Path(__file__).resolve().parent.parent.parent


def resolved_artifact_dir() -> Path:
    """
    过程产物根目录。
    环境变量 TOVEDIO_ARTIFACT_DIR：绝对路径直接使用；相对路径相对 repo_root() 解析。
    未设置时默认为 <repo>/artifacts。
    """
    raw = (os.environ.get("TOVEDIO_ARTIFACT_DIR") or "").strip()
    if not raw:
        return (repo_root() / "artifacts").resolve()
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (repo_root() / p).resolve()


def staging_dir() -> Path:
    """短生命周期辅助文件（如 ffmpeg concat 列表）。"""
    d = resolved_artifact_dir() / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_staging_path(*, prefix: str, suffix: str) -> Path:
    """在 staging 下生成唯一文件路径（调用方写入后应 unlink）。"""
    return staging_dir() / f"{prefix}{uuid.uuid4().hex}{suffix}"


def create_temp_workdir(prefix: str = "tovedio_") -> Path:
    """
    在 artifacts/tmp 下创建唯一工作目录。
    与 tempfile.mkdtemp 相同契约：调用方在 finally 中 shutil.rmtree。
    """
    base = resolved_artifact_dir() / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    sub = base / f"{prefix}{uuid.uuid4().hex}"
    sub.mkdir(parents=False, exist_ok=False)
    return sub
