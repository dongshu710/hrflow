"""控制台编码兼容。

Windows 非中文环境默认 stdout 编码是 cp1252，脚本里的中文 print 会直接抛
UnicodeEncodeError。这里统一重设为 UTF-8，避免"在别人电脑上一跑就崩"。
"""

from __future__ import annotations

import sys


def enable_utf8() -> None:
    """尽力把标准输出切到 UTF-8；不支持 reconfigure 的环境直接跳过。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
