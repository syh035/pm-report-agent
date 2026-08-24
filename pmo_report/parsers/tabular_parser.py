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
    "name":    ["任务", "任务名称", "事项", "交付物", "子任务", "name", "task", "item", "内容", "模块", "工作项", "名称",
                "任务项", "工作内容", "工作事项", "工作", "里程碑"],
    "owner":   ["负责人", "责任单位", "单位", "归属", "owner", "assignee", "责任人", "团队", "部门",
                "责任部门", "负责人/单位", "负责人单位", "承办人", "执行人", "负责部门"],
    "plan_start": ["计划开始", "计划起始", "开始日期", "计划开始时间", "计划开始日期", "start", "plan_start",
                   "planned_start", "开始时间", "start_date", "开工日期", "预计开始"],
    "plan_end":   ["计划完成", "计划结束", "结束日期", "计划完成时间", "计划完成日期", "due", "plan_end",
                   "planned_end", "计划交付", "目标时间", "截止", "截止日期", "截止时间", "deadline", "end",
                   "结束时间", "完成日期", "交付日期", "预计完成"],
    "actual_end": ["实际完成", "实际结束", "完成日期", "actual_end", "完成时间", "实际上线", "实际交付",
                   "actual_end_date", "实际完成日期"],
    "progress":   ["进度", "当前进度", "完成率", "progress", "percent", "pct", "状态进度",
                   "完成进度", "进度百分比", "进度%", "progress_pct", "当前完成率", "完成率%"],
    "status":     ["状态", "status", "当前状态", "阶段", "任务状态", "进展"],
    "note":       ["备注", "说明", "note", "备注说明", "detail", "详情", "备注信息", "补充说明", "描述"],
}


def _map_columns(df: pd.DataFrame, extra_aliases: Dict[str, str] | None = None) -> Dict[str, str]:
    """根据别名（内置 + 用户自定义列名映射），把 DataFrame 列名映射到标准字段名。返回 {标准字段: 实际列名}。"""
    mapping: Dict[str, str] = {}
    # 先建立 小写列名(去空格) -> 原列名
    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            key = str(alias).strip().lower()
            if key in cols_lower:
                mapping[field] = cols_lower[key]
                break
    # 用户自定义列名映射（路线C）：{"表头": "字段"}
    for header, field in (extra_aliases or {}).items():
        key = str(header).strip().lower()
        if key in cols_lower and field in COLUMN_ALIASES:
            mapping[field] = cols_lower[key]
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
    if prog is not None:
        # 兼容多种写法：50、50%、50％（全角）、50 %、0.5（小数比例）、50.0
        s = str(prog).strip().replace("%", "").replace("％", "").replace(" ", "")
        if s.replace(".", "").isdigit():
            try:
                v = float(s)
                # 0 < 进度 ≤ 1 视为小数比例（如 0.5 = 50%），自动放大；1 和 0 保持原义
                if 0 < v < 1:
                    v *= 100
                t.progress = v
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


def _detect_header(df: pd.DataFrame, extra_keys=None) -> Optional[int]:
    """在无表头的 DataFrame 中定位表头行（真实台账常在表头上有标题行/说明行）。
    取前 10 行中命中已知别名（含用户自定义列名）最多的行作为表头；命中 < 2 列视为没有可识别表头。"""
    cols = set()
    for aliases in COLUMN_ALIASES.values():
        for a in aliases:
            cols.add(str(a).strip().lower())
    for k in (extra_keys or {}):
        cols.add(str(k).strip().lower())
    best_row, best_score = None, 0
    for i in range(min(len(df), 10)):
        row = df.iloc[i].astype(str)
        score = sum(1 for c in row if str(c).strip().lower() in cols)
        if score > best_score:
            best_score, best_row = score, i
    if best_score >= 2 and best_row is not None:
        return best_row
    return None


def _apply_header(df: pd.DataFrame, extra_keys=None) -> pd.DataFrame:
    """把检测到的表头行设为列名，并去掉其上的标题/说明行。"""
    if df is None or df.empty:
        return df
    hdr = _detect_header(df, extra_keys)
    if hdr is not None:
        df.columns = df.iloc[hdr].astype(str)
        df = df.iloc[hdr + 1:].reset_index(drop=True)
    return df


def parse_excel(path: str, custom_rules: Dict[str, str] | None = None) -> Project:
    df = pd.read_excel(path, sheet_name=0, header=None)
    return _df_to_project(df, custom_rules=custom_rules)


def parse_excel_all(path: str, custom_rules: Dict[str, str] | None = None) -> List[tuple]:
    """解析 Excel 的所有工作表，返回 [(sheet_name, Project)]（供 Web 面板每表一个 sheet）。"""
    xl = pd.ExcelFile(path)
    out = []
    for name in xl.sheet_names:
        df = pd.read_excel(xl, sheet_name=name, header=None)
        proj = _df_to_project(df, custom_rules=custom_rules)
        proj.name = str(name)
        out.append((str(name), proj))
    return out


def _read_csv_rows(path: str, encodings=("utf-8-sig", "utf-8", "gbk", "gb18030")):
    """用原生 csv.reader 读取（容忍参差行/标题行/空行），返回行列表。自动做编码回退。"""
    import csv as _csv
    for enc in encodings:
        try:
            with open(path, encoding=enc, newline="") as f:
                return [row for row in _csv.reader(f)]
        except UnicodeDecodeError:
            continue
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return [row for row in _csv.reader(f)]


def parse_csv(path: str, encoding: str = "utf-8", custom_rules: Dict[str, str] | None = None) -> Project:
    rows = _read_csv_rows(path, encodings=(encoding, "utf-8-sig", "gbk", "gb18030"))
    if not rows:
        return Project()
    df = pd.DataFrame(rows)
    return _df_to_project(df, custom_rules=custom_rules)


def _df_to_project(df: pd.DataFrame, custom_rules: Dict[str, str] | None = None) -> Project:
    if custom_rules is None:
        from ..rules import custom_column_aliases, custom_ignore_keywords
        extra_aliases = custom_column_aliases()
        ignore = custom_ignore_keywords()
    else:
        extra_aliases = dict(custom_rules.get("column_aliases") or {})
        ignore = list(custom_rules.get("ignore_keywords") or [])
    df = _apply_header(df, extra_keys=extra_aliases)   # 标题行/说明行识别（含自定义列名）
    df = df.dropna(how="all")                          # 去掉全空行
    mapping = _map_columns(df, extra_aliases=extra_aliases)
    tasks: List[Task] = []
    for _, row in df.iterrows():
        t = _row_to_task(row, mapping)
        if t.name:
            if any(kw in t.name for kw in ignore):   # 用户忽略词：任务名命中则跳过
                continue
            tasks.append(t)
    return Project(tasks=tasks)
