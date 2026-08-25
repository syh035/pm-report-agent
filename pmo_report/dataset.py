# -*- coding: utf-8 -*-
"""周报数据集解析：识别"多 sheet 数据集"形态，保留每个板块的完整结构。

背景：PM 周报数据源有两种形态——
  A. 任务进度表（任务/进度/负责人 列），走规则引擎统计；
  B. 周报数据集（每 sheet 一个板块：指标/成果/里程碑/风险/依赖/资源/预算/计划/RFA/附录），
     字段丰富（本周值/上周值/变化/目标/级别/影响/应对/责任人/关闭日期……）。
形态 B 不应被强压成任务表（会丢失字段、表头变任务行），而应保留为结构化板块，
供「模板 + 数据集 → AI 严格按模板抓数生成周报」使用。

本模块：把 xlsx 每个 sheet 解析为 {"section", "headers", "rows", "is_task_table"}。
"""
from __future__ import annotations
import json
from typing import Dict, List, Optional

# 任务表特征列：命中任一即倾向"任务表"
_TASK_COL_HINTS = ("任务", "事项", "工作", "交付", "name", "todo")
_PROGRESS_COL_HINTS = ("进度", "完成", "progress", "完成度")
_OWNER_COL_HINTS = ("负责", "owner", "责任人", "对接人")


def _norm(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    # 去掉科学计数/多余空格
    return s


def _sheet_is_task_table(headers: List[str]) -> bool:
    """启发式：表头里同时有【事项类列 + 进度或负责人列】才视为任务表。"""
    has_task = any(any(h in c for h in _TASK_COL_HINTS) for c in headers)
    has_prog = any(any(h in c for h in _PROGRESS_COL_HINTS) for c in headers)
    has_own = any(any(h in c for h in _OWNER_COL_HINTS) for c in headers)
    return has_task and (has_prog or has_own)


def parse_dataset_sheets(path: str) -> List[Dict]:
    """解析 xlsx 的所有 sheet 为结构化板块。
    返回 [{"section": sheet名, "headers": [...], "rows": [{col: val}...], "is_task_table": bool}]。
    对任一 sheet 解析失败不中断，跳过并记录。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    out: List[Dict] = []
    for ws in wb.worksheets:
        section = str(ws.title)
        grid = []
        for row in ws.iter_rows(values_only=True):
            vals = [_norm(v) for v in row]
            if not any(vals):
                continue
            grid.append(vals)
        if not grid:
            continue
        # 找表头行：第一行非空即表头（数据集通常第一行就是列名）
        header = grid[0]
        headers = [h or f"列{i+1}" for i, h in enumerate(header)]
        rows: List[Dict] = []
        for r in grid[1:]:
            if not any(r):
                continue
            rows.append({headers[i]: r[i] for i in range(min(len(headers), len(r)))})
        out.append({
            "section": section,
            "headers": headers,
            "rows": rows,
            "is_task_table": _sheet_is_task_table(headers),
        })
    return out


def sections_to_json(sections: List[Dict]) -> str:
    """板块结构转紧凑 JSON（供 AI 抓数/前端展示）。"""
    return json.dumps(sections, ensure_ascii=False, indent=1)


def sections_to_markdown(sections: List[Dict], max_rows: int = 200) -> str:
    """板块结构转可读文本（喂给 AI 时比 JSON 更省 token、更易对齐列）。"""
    lines: List[str] = []
    for sec in sections:
        lines.append(f"### 板块：{sec['section']}")
        lines.append("表头：" + " | ".join(sec["headers"]))
        for r in sec["rows"][:max_rows]:
            cells = " | ".join(f"{k}={v}" if v else k for k, v in r.items() if v != "")
            if cells:
                lines.append("  " + cells)
        lines.append("")
    return "\n".join(lines)


def summarize_sections(sections: List[Dict]) -> Dict:
    """板块摘要（源数据详情展示）：每板块行数 + 是否任务表。"""
    return [
        {"section": s["section"], "rows": len(s["rows"]), "is_task_table": s["is_task_table"]}
        for s in sections
    ]


def _section_kind_hint(section: str) -> str:
    """按板块名猜测数据分类（task/risk/issue/decision/milestone/metric/raw）。
    用于把数据集板块分类入仓；猜不准归 raw。"""
    s = section or ""
    if any(k in s for k in ("里程碑", "milestone")):
        return "milestone"
    if any(k in s for k in ("风险", "问题", "risk")):
        return "risk"
    if any(k in s for k in ("决策", "申请", "approval", "RFA", "rfa")):
        return "decision"
    if any(k in s for k in ("指标", "看板", "数据", "metric", "kpi")):
        return "metric"
    if any(k in s for k in ("成果", "计划", "任务", "进展", "todo", "task")):
        return "task"
    if any(k in s for k in ("依赖", "协作", "issue")):
        return "issue"
    return "raw"
