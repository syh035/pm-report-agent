# -*- coding: utf-8 -*-
"""
规则引擎：对统一的项目进度模型做统计计算与风险分级。

输入 Project（含任务列表），输出 Stats 统计结果，供 AI 周报生成与展示使用。
本层是纯规则、确定性、不依赖网络。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Tuple, Optional

from .models import Project, Task, STATUS_DONE, STATUS_IN_PROGRESS, STATUS_NOT_STARTED, STATUS_DELAYED, STATUS_RISK
from .rules import DEFAULT_RULES


# 向后兼容的模块级默认（保留旧引用）
RISK_DELAY_DAYS_DANGER = DEFAULT_RULES["delay_days_danger"]
RISK_NEAR_END_DAYS = DEFAULT_RULES["risk_near_end_days"]
RISK_SLOW_PROGRESS_PCT = DEFAULT_RULES["slow_progress_pct"]


@dataclass
class TaskStat:
    task: Task
    delay_days: int = 0        # 与计划相比滞后/提前天数（正=滞后）
    is_late: bool = False       # 是否已滞后
    risk_level: str = "正常"    # 正常 / 关注 / 风险
    risk_reason: str = ""
    target_progress: Optional[float] = None  # 目标进度%（线性日期比例，0-100）
    progress_gap: Optional[float] = None    # target - actual（正=实际落后于目标）

    def to_dict(self) -> Dict:
        d = {
            "task": self.task.to_dict(),
            "delay_days": self.delay_days,
            "is_late": self.is_late,
            "risk_level": self.risk_level,
            "risk_reason": self.risk_reason,
            "target_progress": round(self.target_progress, 1) if self.target_progress is not None else None,
            "progress_gap": round(self.progress_gap, 1) if self.progress_gap is not None else None,
        }
        return d


@dataclass
class ProjectStats:
    project: Project
    total_tasks: int = 0
    done_count: int = 0          # 已完成
    in_progress_count: int = 0   # 进行中
    not_started_count: int = 0   # 未开始
    delayed_count: int = 0       # 已滞后
    risk_count: int = 0          # 有风险
    completion_rate: float = 0.0 # 整体完成率（0-100）
    avg_progress: float = 0.0    # 平均实际进度
    avg_target_progress: Optional[float] = None  # 平均目标进度
    task_stats: "List[TaskStat]" = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "project_name": self.project.name,
            "period": self.project.period,
            "total": self.total_tasks,
            "done": self.done_count,
            "in_progress": self.in_progress_count,
            "not_started": self.not_started_count,
            "delayed": self.delayed_count,
            "risk": self.risk_count,
            "completion_rate": round(self.completion_rate, 1),
            "avg_progress": round(self.avg_progress, 1),
            "avg_target_progress": round(self.avg_target_progress, 1) if self.avg_target_progress is not None else None,
            "tasks": [
                {
                    "name": ts.task.name,
                    "owner": ts.task.owner,
                    "progress": ts.task.progress,
                    "status": ts.task.status or "",
                    "is_late": ts.is_late,
                    "risk_level": ts.risk_level,
                    "risk_reason": ts.risk_reason,
                    "target_progress": round(ts.target_progress, 1) if ts.target_progress is not None else None,
                    "progress_gap": round(ts.progress_gap, 1) if ts.progress_gap is not None else None,
                    "plan_end": ts.task.plan_end.isoformat() if ts.task.plan_end else None,
                }
                for ts in self.task_stats
            ],
        }


def _eval_task(t: Task, today: date | None = None, rules: Dict | None = None) -> TaskStat:
    """评估单个任务：滞后天数、风险等级。rules 为可配置阈值。"""
    today = today or date.today()
    rules = {**DEFAULT_RULES, **(rules or {})}
    delay_danger = rules.get("delay_days_danger", DEFAULT_RULES["delay_days_danger"])
    near_end = rules.get("risk_near_end_days", DEFAULT_RULES["risk_near_end_days"])
    slow_pct = rules.get("slow_progress_pct", DEFAULT_RULES["slow_progress_pct"])

    st = TaskStat(task=t)
    if not t.name:
        return st

    # 状态归一化
    status = (t.status or "").strip()
    if t.progress is not None and t.progress >= 100:
        status = STATUS_DONE
    elif t.actual_end is not None:
        status = STATUS_DONE
    elif t.progress is not None and t.progress <= 0:
        status = STATUS_NOT_STARTED
    elif t.progress is not None:
        status = STATUS_IN_PROGRESS

    # 滞后判断：今天 > 计划完成 且 未完成
    if status != STATUS_DONE and t.plan_end is not None:
        st.delay_days = (today - t.plan_end).days
        if st.delay_days > 0:
            st.is_late = True

    # 目标进度（线性日期比例）：(today - plan_start) / (plan_end - plan_start)
    if t.plan_start and t.plan_end:
        span = (t.plan_end - t.plan_start).days
        if span > 0:
            passed = (today - t.plan_start).days
            target = max(0.0, min(passed / span * 100, 100.0))
            st.target_progress = target
            if t.progress is not None:
                st.progress_gap = target - t.progress  # 正=实际落后于目标

    # 风险分级
    reasons = []
    if status == STATUS_DONE:
        st.risk_level = "正常"
        # 已完成任务进度视为满
        st.target_progress = 100.0 if st.target_progress is None else st.target_progress
    else:
        if st.delay_days > 0:
            reasons.append(f"超期 {st.delay_days} 天")
            if st.delay_days >= delay_danger:
                st.risk_level = "风险"
            else:
                st.risk_level = "关注"
        elif t.plan_end is not None:
            remain = (t.plan_end - today).days
            if 0 <= remain <= near_end and status != STATUS_NOT_STARTED:
                reasons.append(f"临近计划完成（剩 {remain} 天）")
                st.risk_level = max(st.risk_level, "关注")
            if remain < 0:
                st.risk_level = "风险"
        # 进度偏慢：基于目标进度与实际进度的差距
        if st.progress_gap is not None and st.progress_gap > slow_pct:
            reasons.append(f"进度偏慢（落后目标 {st.progress_gap:.0f}%）")
            if st.risk_level == "正常":
                st.risk_level = "关注"
    st.risk_reason = "；".join(reasons)
    return st


def analyze(project: Project, today: date | None = None, rules: Dict | None = None) -> ProjectStats:
    """分析整个项目。rules 为可配置阈值（None 时用默认）。"""
    today = today or date.today()
    stats = ProjectStats(project=project)
    stats.total_tasks = len(project.tasks)
    if not project.tasks:
        return stats

    prog_sum = 0.0
    for t in project.tasks:
        ts = _eval_task(t, today, rules)
        prog = t.progress or 0.0
        prog_sum += prog
        status = ts.task.status or ""
        # 用归一化后的状态计数
        st_norm = ts.task.status = ts.task.status or ""
        if t.progress is not None and t.progress >= 100:
            status = STATUS_DONE
        else:
            status = st_norm
        if status == STATUS_DONE:
            stats.done_count += 1
        elif status == STATUS_IN_PROGRESS:
            stats.in_progress_count += 1
        elif status == STATUS_NOT_STARTED:
            stats.not_started_count += 1
        elif status == STATUS_DELAYED:
            stats.delayed_count += 1
        elif status == STATUS_RISK:
            stats.risk_count += 1
        elif ts.is_late:
            # 未在状态字段标注且滞后的归为已滞后
            stats.delayed_count += 1
        elif ts.risk_level != "正常":
            stats.risk_count += 1
        else:
            stats.in_progress_count += 1
        stats.task_stats.append(ts)

    stats.avg_progress = round(prog_sum / stats.total_tasks, 1)
    # 完成率 = 已完成任务数 / 总数
    stats.completion_rate = round(stats.done_count / stats.total_tasks * 100, 1)
    # 风险任务数（level != 正常）
    stats.risk_count = sum(1 for ts in stats.task_stats if ts.risk_level != "正常")
    # 平均目标进度（仅统计有目标进度的任务）
    targets = [ts.target_progress for ts in stats.task_stats if ts.target_progress is not None]
    if targets:
        stats.avg_target_progress = round(sum(targets) / len(targets), 1)
    return stats
