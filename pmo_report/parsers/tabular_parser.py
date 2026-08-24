# -*- coding: utf-8 -*-
"""
结构化表格解析器：CSV / Excel。

读取一张「任务进度表」，把每一行归一化为一个 Task。
期望的列（支持常见别名，见 COLUMN_ALIASES）。缺失列可容忍，用默认值。
"""
from __future__ import annotations
from datetime import date, datetime
from typing import Any, Dict, Optional, List

import pandas as pd

from ..models import Project, Task, STATUS_NOT_STARTED, STATUS_IN_PROGRESS, STATUS_DONE
from ._date_util import parse_date


# 列名 → 字段 的别名映射（大小写、常见中文名）
COLUMN_ALIASES = {
    "name":    ["任务", "任务名称", "事项", "交付物", "子任务", "name", "task", "item", "内容", "模块", "工作项", "名称"],
    "owner":   ["负责人", "责任单位", "单位", "归属", "owner", "assignee", "责任人", "团队", "部门"],
    "plan_start": ["计划开始", "计划起始", "开始日期", "计划开始时间", "start", "plan_start", "planned_start"],
    "plan_end":   ["计划完成", "计划结束", "结束日期", "计划完成时间", "due", "plan_end", "planned_end", "计划交付", "目标时间"],
    "actual_end": ["实际完成", "实际结束", "完成日期", "actual_end", "完成时间", "实际上线"],
    "progress":   ["进度", "当前进度", "完成率", "progress", "percent", "pct", "状态进度"],
    "status":     ["状态", "status", "当前状态", "阶段"],
    "note":       ["备注", "说明", "note", "备注说明", "detail", "详情"],
}


def _map_columns(df: pd.DataFrame) -> Dict[str, str]:
    """根据别名，把 DataFrame 列名映射到标准字段名。返回 {标准字段: 实际列名}。"""
    mapping: Dict[str, str] = {}
    # 先建立 小写列名(去空格) -> 原列名
    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            key = str(alias).strip().lower()
            if key in cols_lower:
                mapping[field] = cols_lower[key]
                break
    return mapping


def _row_to_task(row: pd.Series, mapping: Dict[str, str]) -> Task:
    """把一行 DataFrame 转成 Task，容忍缺失。"""
    def get(field: str, default: Any = None) -> Any:
        col = mapping.get(field)
        if col is None or pd.isna(row.get(col)):
            return default
        return row[col]

    t = Task()
    t.name = str(get("name", ""))
    if not t.name:
        # 无名称时不跳过，让上层过滤；尽量取第一列非空值
        for c in row.index:
            if pd.notna(row[c]) and str(row[c]).strip():
                t.name = str(row[c]).strip()
                break
    t.owner = str(get("owner", "")) if get("owner") else ""
    t.plan_start = parse_date(get("plan_start"))
    t.plan_end = parse_date(get("plan_end"))
    t.actual_end = parse_date(get("actual_end"))

    prog = get("progress")
    if prog is not None and str(prog).replace("%", "").strip().replace(".", "").isdigit():
        try:
            t.progress = float(str(prog).replace("%", "").strip())
        except ValueError:
            t.progress = None

    t.status = str(get("status", "")).strip() if get("status") else ""
    if not t.status and t.progress is not None and t.actual_end:
        # 由进度推导状态
        if t.progress >= 100:
            t.status = STATUS_DONE
        elif t.progress > 0:
            t.status = STATUS_IN_PROGRESS
        else:
            t.status = STATUS_NOT_STARTED
    t.note = str(get("note", "")) if get("note") else ""
    return t


def parse_excel(path: str) -> Project:
    df = pd.read_excel(path, sheet_name=0)
    return _df_to_project(df)


def parse_csv(path: str, encoding: str = "utf-8") -> Project:
    # 兼容常见编码
    for enc in (encoding, "gbk", "gb18030"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        df = pd.read_csv(path, encoding="utf-8", errors="replace")
    return _df_to_project(df)


def _df_to_project(df: pd.DataFrame) -> Project:
    df = df.dropna(how="all")  # 去掉全空行
    mapping = _map_columns(df)
    tasks: List[Task] = []
    for _, row in df.iterrows():
        t = _row_to_task(row, mapping)
        if t.name:
            tasks.append(t)
    return Project(tasks=tasks)
