# -*- coding: utf-8 -*-
"""
数据模型：统一的"项目进度模型"。

无论输入来自 Excel/CSV/Word/PDF，最终都归一化为一个 Project 对象，
包含一组 Task（进度条目）。规则引擎和 AI 生成都基于这个模型工作。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict
from datetime import date


# 任务状态枚举
STATUS_NOT_STARTED = "未开始"
STATUS_IN_PROGRESS = "进行中"
STATUS_DONE = "已完成"
STATUS_DELAYED = "已滞后"
STATUS_RISK = "有风险"

# 标准状态集合（供解析层归一化用）
ALL_STATUS = {STATUS_NOT_STARTED, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_DELAYED, STATUS_RISK}


@dataclass
class Task:
    """一条项目进度条目。字段尽量宽松，缺失项为 None。"""
    name: str = ""                    # 任务/交付物名称
    owner: str = ""                   # 负责人 / 单位
    plan_start: Optional[date] = None # 计划开始
    plan_end: Optional[date] = None   # 计划完成
    actual_end: Optional[date] = None # 实际完成
    progress: Optional[float] = None  # 当前进度 0-100
    status: str = ""                  # 原始状态文本（未归一化时保留）
    note: str = ""                    # 备注 / 详情
    depends_on: str = ""              # 依赖的任务名（风险传递用，可空）
    slow_ok: bool = False             # 用户标记：该任务进度偏慢属正常，不再标红

    def to_dict(self) -> Dict:
        d = asdict(self)
        # 日期转字符串，方便序列化给 AI/展示
        for k in ("plan_start", "plan_end", "actual_end"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


@dataclass
class Project:
    """一个项目的完整信息，含任务列表与来源元数据。"""
    name: str = "未命名项目"
    period: str = ""                  # 统计周期（周），如 "2026-W33"
    source_files: List[str] = field(default_factory=list)  # 输入来源文件
    tasks: List[Task] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "period": self.period,
            "source_files": self.source_files,
            "tasks": [t.to_dict() for t in self.tasks],
        }
