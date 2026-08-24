# -*- coding: utf-8 -*-
"""日期解析工具：把各种格式的字符串/数值转为 datetime.date。"""
from __future__ import annotations
from datetime import date, datetime
from typing import Optional, Any
import re


def parse_date(value: Any) -> Optional[date]:
    """尽力把输入转成 date。无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # Excel 序列日期：1970 之后的天数偏移（简单近似，来自 openpyxl/pandas 已转 datetime）
        try:
            return date.fromordinal(int(value) + date(1899, 12, 30).toordinal())
        except (ValueError, OverflowError):
            return None

    s = str(value).strip()
    if not s:
        return None

    # 常见格式
    patterns = [
        r"(\d{4})-(\d{1,2})-(\d{1,2})",           # 2026-08-16
        r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})",     # 2026/8/16, 2026.8.16
        r"(\d{4})年(\d{1,2})月(\d{1,2})日?",       # 2026年8月16日
        r"(\d{1,2})[/](\d{1,2})[/](\d{2,4})",     # 8/16/2026
        r"(\d{4})-(\d{1,2})",                     # 2026-08 年月
        r"(\d{4})[/.](\d{1,2})",                  # 2026.08 年月
    ]
    for pat in patterns:
        m = re.search(pat, s)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if y < 100:  # 两位年归为 2000 年代
                    y += 2000
                return date(y, mo, d)
            except ValueError:
                continue

    # 年月（补 1 日）
    m = re.search(r"(\d{4})[-/.年](\d{1,2})", s)
    if m:
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            if y < 100:
                y += 2000
            return date(y, mo, 1)
        except ValueError:
            return None
    return None
