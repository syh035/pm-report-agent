# -*- coding: utf-8 -*-
"""
规则引擎：对统一的项目进度模型做统计计算与风险分级。

输入 Project（含任务列表），输出 Stats 统计结果，供 AI 周报生成与展示使用。
本层是纯规则、确定性、不依赖网络。
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Tuple, Optional

from .models import Project, Task, STATUS_DONE, STATUS_IN_PROGRESS, STATUS_NOT_STARTED, STATUS_DELAYED, STATUS_RISK
from .rules import DEFAULT_RULES


# 向后兼容的模块级默认（保留旧引用）
RISK_DELAY_DAYS_DANGER = DEFAULT_RULES["delay_days_danger"]
RISK_NEAR_END_DAYS = DEFAULT_RULES["risk_near_end_days"]
RISK_SLOW_PROGRESS_PCT = DEFAULT_RULES["slow_progress_pct"]

# 状态变体词 → 标准状态（文件里的各种叫法归一化）
_STATUS_NORM = {
    "已完成": STATUS_DONE, "完成": STATUS_DONE, "已上线": STATUS_DONE, "已交付": STATUS_DONE,
    "已结束": STATUS_DONE, "完结": STATUS_DONE,
    "进行中": STATUS_IN_PROGRESS, "推进中": STATUS_IN_PROGRESS, "实施中": STATUS_IN_PROGRESS,
    "开发中": STATUS_IN_PROGRESS, "进行": STATUS_IN_PROGRESS,
    "未开始": STATUS_NOT_STARTED, "待开始": STATUS_NOT_STARTED, "未启动": STATUS_NOT_STARTED,
    "未开展": STATUS_NOT_STARTED, "待启动": STATUS_NOT_STARTED,
    "已滞后": STATUS_DELAYED, "滞后": STATUS_DELAYED, "延期": STATUS_DELAYED, "已延期": STATUS_DELAYED,
    "有风险": STATUS_RISK, "风险": STATUS_RISK, "高风险": STATUS_RISK, "告警": STATUS_RISK,
}


def normalize_status(raw: str, extra: Dict[str, str] | None = None) -> str:
    """把文件里的状态文本（含变体）归一化为标准状态；无法识别返回原文本。
    extra：用户自定义状态词映射（规则库 status_words）。"""
    s = (raw or "").strip()
    if not s:
        return ""
    m = _STATUS_NORM.get(s)
    if m:
        return m
    if extra:
        return extra.get(s, s)
    return s


# 风险等级排序（不能用字符串 max()：中文比较会出错，如 max("正常","关注") 会错误返回"正常"）
_RISK_LEVELS = {"正常": 0, "关注": 1, "风险": 2}


def _raise_level(level: str, new: str) -> str:
    """取更高风险等级。"""
    return new if _RISK_LEVELS.get(new, 0) > _RISK_LEVELS.get(level, 0) else level


def _target_progress(passed: int, span: int, curve: str = "linear") -> float:
    """按曲线计算目标进度（0-100）。
    - linear：时间线性比例
    - s_curve：逻辑斯蒂 S 型（前期慢、中期快、后期收敛），更贴近真实项目节奏
    """
    x = max(0.0, min(passed / span, 1.0)) if span > 0 else 0.0
    if curve == "s_curve":
        t = 1.0 / (1.0 + math.exp(-10.0 * (x - 0.5)))
        return max(0.0, min(t * 100.0, 100.0))
    return x * 100.0


@dataclass
class TaskStat:
    task: Task
    delay_days: int = 0        # 与计划相比滞后/提前天数（正=滞后）
    is_late: bool = False       # 是否已滞后
    risk_level: str = "正常"    # 正常 / 关注 / 风险
    risk_reason: str = ""
    target_progress: Optional[float] = None  # 目标进度%（线性日期比例，0-100）
    progress_gap: Optional[float] = None    # target - actual（正=实际落后于目标）
    status_norm: str = ""       # 归一化后的标准状态（供筛选/显示统一使用）
    is_critical: bool = False   # 人工标注：关键任务（任务表勾选「关键」）

    def to_dict(self) -> Dict:
        d = {
            "task": self.task.to_dict(),
            "delay_days": self.delay_days,
            "is_late": self.is_late,
            "risk_level": self.risk_level,
            "risk_reason": self.risk_reason,
            "target_progress": round(self.target_progress, 1) if self.target_progress is not None else None,
            "progress_gap": round(self.progress_gap, 1) if self.progress_gap is not None else None,
            "status_norm": self.status_norm,
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
    health: Dict = field(default_factory=dict)   # 数据体检：缺失字段清单

    def to_dict(self) -> Dict:
        d = {
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
            "health": self.health,
            "tasks": [
                {
                    "name": ts.task.name,
                    "owner": ts.task.owner,
                    "progress": ts.task.progress,
                    "status": ts.task.status or "",
                    "status_norm": ts.status_norm or "",
                    "is_late": ts.is_late,
                    "is_critical": ts.is_critical,
                    "risk_level": ts.risk_level,
                    "risk_reason": ts.risk_reason,
                    "target_progress": round(ts.target_progress, 1) if ts.target_progress is not None else None,
                    "progress_gap": round(ts.progress_gap, 1) if ts.progress_gap is not None else None,
                    "plan_end": ts.task.plan_end.isoformat() if ts.task.plan_end else None,
                }
                for ts in self.task_stats
            ],
        }
        return d


def _eval_task(t: Task, today: date | None = None, rules: Dict | None = None) -> TaskStat:
    """评估单个任务：滞后天数、风险等级。rules 为可配置阈值。"""
    today = today or date.today()
    rules = {**DEFAULT_RULES, **(rules or {})}
    delay_danger = rules.get("delay_days_danger", DEFAULT_RULES["delay_days_danger"])
    near_end = rules.get("risk_near_end_days", DEFAULT_RULES["risk_near_end_days"])
    slow_pct = rules.get("slow_progress_pct", DEFAULT_RULES["slow_progress_pct"])
    curve = rules.get("progress_curve", "linear")

    st = TaskStat(task=t)
    if not t.name:
        return st

    # 状态归一化：显式状态（含变体词+用户自定义词）优先；无显式状态时按进度/完成日期推导
    raw_status = (t.status or "").strip()
    extra_words = rules.get("status_words") or {}   # 用户自定义状态词（规则库）
    explicit = normalize_status(raw_status, extra_words) if raw_status else ""
    if explicit in (STATUS_DONE, STATUS_DELAYED, STATUS_RISK):
        # 文件里明确标注的「已完成/已滞后/有风险」优先（人工判断 > 自动推导）
        status = explicit
    elif t.actual_end is not None:
        status = STATUS_DONE
    elif t.progress is not None and t.progress >= 100:
        status = STATUS_DONE
    elif t.progress is not None and t.progress <= 0:
        status = STATUS_NOT_STARTED
    elif t.progress is not None:
        status = STATUS_IN_PROGRESS
    elif explicit:
        status = explicit
    else:
        # 无进度且无显式状态：保守视为「未开始」，便于触发未按时启动预警
        status = STATUS_NOT_STARTED
    st.status_norm = status or explicit

    # 滞后判断：今天 > 计划完成 且 未完成
    if status != STATUS_DONE and t.plan_end is not None:
        st.delay_days = (today - t.plan_end).days
        if st.delay_days > 0:
            st.is_late = True

    # 目标进度（按可配置曲线）
    if t.plan_start and t.plan_end:
        span = (t.plan_end - t.plan_start).days
        if span > 0:
            passed = (today - t.plan_start).days
            target = _target_progress(passed, span, curve)
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
        # 显式标注「有风险」：直接给最高级，尊重文件里的人工判断
        if status == STATUS_RISK:
            st.risk_level = _raise_level(st.risk_level, "风险")
            reasons.append("状态标注为有风险")
        if st.delay_days > 0:
            reasons.append(f"超期 {st.delay_days} 天")
            if st.delay_days >= delay_danger:
                st.risk_level = _raise_level(st.risk_level, "风险")
            else:
                st.risk_level = _raise_level(st.risk_level, "关注")
        elif t.plan_end is not None:
            remain = (t.plan_end - today).days
            if 0 <= remain <= near_end and status != STATUS_NOT_STARTED:
                reasons.append(f"临近计划完成（剩 {remain} 天）")
                st.risk_level = _raise_level(st.risk_level, "关注")
        # 未按时启动：计划开始已过仍未开始 → 关注
        if status == STATUS_NOT_STARTED and t.plan_start is not None:
            start_delay = (today - t.plan_start).days
            if start_delay > 0:
                reasons.append(f"未按计划启动（晚 {start_delay} 天）")
                st.risk_level = _raise_level(st.risk_level, "关注")
        # 显式标注「已滞后」：至少关注
        if status == STATUS_DELAYED and st.risk_level == "正常":
            st.risk_level = _raise_level(st.risk_level, "关注")
            reasons.append("状态标注为已滞后")
        # 进度偏慢：基于目标进度与实际进度的差距（用户标记 slow_ok 的任务豁免）
        if st.progress_gap is not None and st.progress_gap > slow_pct and not t.slow_ok:
            reasons.append(f"进度偏慢（落后目标 {st.progress_gap:.0f}%）")
            if st.risk_level == "正常":
                st.risk_level = _raise_level(st.risk_level, "关注")
    st.risk_reason = "；".join(reasons)
    return st


def _apply_dependencies(stats: "ProjectStats") -> None:
    """依赖风险传递：若任务依赖的对象滞后/有风险，本任务至少标为「关注」。"""
    by_name = {ts.task.name: ts for ts in stats.task_stats if ts.task.name}
    for ts in stats.task_stats:
        dep = (ts.task.depends_on or "").strip()
        if not dep:
            continue
        dts = by_name.get(dep)
        if dts is not None and (dts.is_late or dts.risk_level != "正常") and ts.risk_level != "风险":
            ts.risk_level = _raise_level(ts.risk_level, "关注")
            reason = f"依赖项「{dep}」滞后/有风险"
            ts.risk_reason = (ts.risk_reason + "；" + reason).strip("；") if ts.risk_reason else reason


def _health_check(stats: "ProjectStats") -> Dict:
    """数据体检：统计缺失关键字段/异常数据的任务，便于用户补数据。"""
    tss = stats.task_stats
    h = {
        "total": stats.total_tasks,
        "missing_progress": [ts.task.name for ts in tss if ts.task.progress is None],
        "missing_owner": [ts.task.name for ts in tss if not (ts.task.owner or "").strip()],
        "missing_plan_start": [ts.task.name for ts in tss if ts.task.plan_start is None],
        "missing_plan_end": [ts.task.name for ts in tss if ts.task.plan_end is None],
        "over_progress": [ts.task.name for ts in tss if (ts.task.progress or 0) > 100],
        "date_inverted": [ts.task.name for ts in tss
                          if ts.task.plan_start and ts.task.plan_end and ts.task.plan_end < ts.task.plan_start],
        "short_name": [ts.task.name for ts in tss if ts.task.name and len(ts.task.name) <= 1],
    }
    return h


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
    # 完成率：默认 已完成数/总数；若配置了任务权重（≠1），按权重计算
    wsum = sum(t.weight for t in project.tasks if t.weight and t.weight > 0)
    if wsum and abs(wsum - stats.total_tasks) > 1e-9:
        done_weight = sum(ts.task.weight for ts in stats.task_stats if ts.status_norm == STATUS_DONE)
        stats.completion_rate = round(done_weight / wsum * 100, 1)
        wprog = sum((ts.task.progress or 0.0) * ts.task.weight for ts in stats.task_stats)
        stats.avg_progress = round(wprog / wsum, 1)
    else:
        # 完成率 = 已完成任务数 / 总数
        stats.completion_rate = round(stats.done_count / stats.total_tasks * 100, 1)
    # 依赖风险传递（先于风险统计执行，使传递来的「关注」计入风险数）
    _apply_dependencies(stats)
    # 关键任务：人工标注（任务表勾选「关键」），不再自动计算
    for ts in stats.task_stats:
        ts.is_critical = bool(ts.task.critical)
    # 风险任务数（level != 正常）
    stats.risk_count = sum(1 for ts in stats.task_stats if ts.risk_level != "正常")
    # 平均目标进度（仅统计有目标进度的任务）
    targets = [ts.target_progress for ts in stats.task_stats if ts.target_progress is not None]
    if targets:
        stats.avg_target_progress = round(sum(targets) / len(targets), 1)
    # 数据体检
    stats.health = _health_check(stats)
    return stats
