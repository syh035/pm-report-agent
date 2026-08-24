# -*- coding: utf-8 -*-
"""引擎规则单元测试：风险分级、显式状态、依赖传递、豁免、曲线等确定性逻辑。
用法：.venv/bin/python tests/test_engine_rules.py（纯本地，不依赖网络/AI）"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmo_report.engine import analyze, normalize_status, _raise_level
from pmo_report.models import Project, Task

TODAY = date(2026, 8, 24)


def test_not_started_warning():
    """计划开始已过仍未开始 → 关注 + 未按计划启动。"""
    p = Project(tasks=[Task(name="a", plan_start=date(2026, 8, 14), plan_end=date(2026, 9, 14))])
    ts = analyze(p, today=TODAY).task_stats[0]
    assert ts.risk_level == "关注", f"应为关注，实际 {ts.risk_level}"
    assert "未按计划启动" in ts.risk_reason, ts.risk_reason
    print("✅ 未按时启动预警")


def test_explicit_delayed_status():
    """显式状态「已滞后」（即使日期未超期）→ 至少关注。"""
    p = Project(tasks=[Task(name="b", status="已滞后", plan_end=date(2026, 9, 30))])
    ts = analyze(p, today=TODAY).task_stats[0]
    assert ts.status_norm == "已滞后"
    assert ts.risk_level == "关注"
    assert "状态标注为已滞后" in ts.risk_reason
    print("✅ 显式已滞后 → 关注")


def test_explicit_risk_status():
    """显式状态「有风险」→ 风险。"""
    p = Project(tasks=[Task(name="c", status="有风险")])
    ts = analyze(p, today=TODAY).task_stats[0]
    assert ts.risk_level == "风险"
    print("✅ 显式有风险 → 风险")


def test_explicit_done_wins_over_progress():
    """显式「已上线」+ 进度 30 → 尊重人工标注为已完成。"""
    p = Project(tasks=[Task(name="d", status="已上线", progress=30)])
    ts = analyze(p, today=TODAY).task_stats[0]
    assert ts.status_norm == "已完成", ts.status_norm
    assert ts.risk_level == "正常"
    print("✅ 显式已完成优先于进度")


def test_status_variant_normalization():
    """状态变体词归一化。"""
    assert normalize_status("已交付") == "已完成"
    assert normalize_status("延期") == "已滞后"
    assert normalize_status("高风险") == "有风险"
    assert normalize_status("推进中") == "进行中"
    assert normalize_status("未知词") == "未知词"
    print("✅ 状态变体词归一化")


def test_near_deadline_upgrades_to_warning():
    """临近计划完成 → 关注（修复 max 中文比较 bug 的回归保护）。"""
    p = Project(tasks=[Task(name="e", progress=50, plan_start=date(2026, 7, 1), plan_end=date(2026, 8, 26))])
    ts = analyze(p, today=TODAY).task_stats[0]
    assert ts.risk_level == "关注", ts.risk_level
    assert "临近计划完成" in ts.risk_reason
    print("✅ 临近完成 → 关注")


def test_slow_ok_exemption():
    """偏慢豁免：勾选 slow_ok 的任务进度偏慢不再标红（超期仍照判）。"""
    # 目标进度约 90%，实际 10% → 差距 80% > 40% 阈值，本应标偏慢
    p = Project(tasks=[Task(name="f", progress=10, plan_start=date(2026, 7, 1),
                            plan_end=date(2026, 8, 30), slow_ok=True)])
    ts = analyze(p, today=TODAY).task_stats[0]
    assert "进度偏慢" not in ts.risk_reason, ts.risk_reason
    assert ts.risk_level == "正常"
    # 同样数据但不豁免 → 应标偏慢
    p2 = Project(tasks=[Task(name="f", progress=10, plan_start=date(2026, 7, 1),
                             plan_end=date(2026, 8, 30))])
    ts2 = analyze(p2, today=TODAY).task_stats[0]
    assert "进度偏慢" in ts2.risk_reason
    assert ts2.risk_level == "关注"
    print("✅ 偏慢豁免")


def test_dependency_propagation():
    """依赖传递：被依赖任务滞后 → 本任务标关注并注明原因。"""
    p = Project(tasks=[
        Task(name="A", progress=20, plan_start=date(2026, 8, 1), plan_end=date(2026, 8, 15)),
        Task(name="B", progress=60, depends_on="A"),
    ])
    stats = analyze(p, today=TODAY)
    ts_b = next(ts for ts in stats.task_stats if ts.task.name == "B")
    assert ts_b.risk_level == "关注", ts_b.risk_level
    assert "依赖项「A」" in ts_b.risk_reason
    print("✅ 依赖风险传递")


def test_raise_level_semantics():
    """风险等级比较（修复前的 max 字符串比较 bug）。"""
    assert _raise_level("正常", "关注") == "关注"
    assert _raise_level("关注", "风险") == "风险"
    assert _raise_level("风险", "关注") == "风险"
    assert _raise_level("正常", "正常") == "正常"
    print("✅ 风险等级比较")


def test_progress_curve_affects_target():
    """进度曲线切换会改变目标进度。"""
    p = Project(tasks=[Task(name="g", progress=30, plan_start=date(2026, 8, 1), plan_end=date(2026, 9, 15))])
    linear = analyze(p, today=TODAY, rules={"progress_curve": "linear"}).task_stats[0].target_progress
    s_curve = analyze(p, today=TODAY, rules={"progress_curve": "s_curve"}).task_stats[0].target_progress
    assert linear is not None and s_curve is not None
    assert abs(linear - s_curve) > 1.0, f"线性={linear} S型={s_curve}"
    print(f"✅ 曲线影响目标进度（线性 {linear:.0f}% vs S型 {s_curve:.0f}%）")


def test_status_norm_exposed():
    """status_norm 出现在统计输出中，供前端筛选。"""
    p = Project(tasks=[Task(name="h", status="已上线")])
    d = analyze(p, today=TODAY).to_dict()
    assert d["tasks"][0]["status_norm"] == "已完成"
    print("✅ status_norm 透出")


def test_no_progress_no_status_defaults_to_not_started():
    """无进度无状态 + 计划开始已过 → 未开始 + 启动预警（保守默认）。"""
    p = Project(tasks=[Task(name="i", plan_start=date(2026, 8, 10), plan_end=date(2026, 9, 10))])
    ts = analyze(p, today=TODAY).task_stats[0]
    assert ts.status_norm == "未开始"
    assert "未按计划启动" in ts.risk_reason
    print("✅ 无数据默认未开始 + 启动预警")


def test_weighted_completion():
    """任务权重影响完成率：A 完成(权重3) + B 未完成(权重1) → 75%；无权重时 50%。"""
    p = Project(tasks=[Task(name="A", progress=100, weight=3), Task(name="B", progress=0, weight=1)])
    s = analyze(p, today=TODAY)
    assert s.completion_rate == 75.0, s.completion_rate
    p2 = Project(tasks=[Task(name="A", progress=100), Task(name="B", progress=0)])
    assert analyze(p2, today=TODAY).completion_rate == 50.0
    print("✅ 任务权重完成率")


def test_manual_critical_mark():
    """关键任务改为人工标注：勾选 critical 才标 is_critical；不再自动计算 critical_path。"""
    p = Project(tasks=[Task(name="A", critical=True), Task(name="B", critical=False)])
    s = analyze(p, today=TODAY)
    crit = {ts.task.name for ts in s.task_stats if ts.is_critical}
    assert crit == {"A"}, crit
    d = s.to_dict()
    assert "critical_path" not in d, "已移除自动关键路径字段"
    assert d["tasks"][0]["is_critical"] is True
    assert d["tasks"][1]["is_critical"] is False
    print("✅ 人工关键标注（不再自动计算）")


if __name__ == "__main__":
    test_not_started_warning()
    test_explicit_delayed_status()
    test_explicit_risk_status()
    test_explicit_done_wins_over_progress()
    test_status_variant_normalization()
    test_near_deadline_upgrades_to_warning()
    test_slow_ok_exemption()
    test_dependency_propagation()
    test_raise_level_semantics()
    test_progress_curve_affects_target()
    test_status_norm_exposed()
    test_no_progress_no_status_defaults_to_not_started()
    test_weighted_completion()
    test_manual_critical_mark()
    print("\n全部通过 ✅")
