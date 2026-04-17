#!/usr/bin/env python3
"""
从仓库根目录运行 toVedio（优先使用 src 布局入口）。

用法示例（novel.txt 请换为你的 UTF-8 小说路径）:
  py -3 run_tovedio.py novel.txt -o artifacts/exports/out.mp4 --mode l2v --style real
  py -3 run_tovedio.py novel.txt -o artifacts/exports/out.mp4 --mode t2v --style real
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    src = root / "src"
    has_src_cli = (src / "tovedio" / "cli.py").is_file()
    if not has_src_cli:
        print("错误：请在 toVedio 仓库根目录运行此脚本。", file=sys.stderr)
        return 2
    sys.path.insert(0, str(src))
    from tovedio.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
