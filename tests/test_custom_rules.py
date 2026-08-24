# -*- coding: utf-8 -*-
"""路线C 自定义规则库单元测试：忽略词 / 自定义列名映射 / 自定义状态词映射 / 校对回写。
用法：.venv/bin/python tests/test_custom_rules.py（纯本地，不依赖网络/AI）"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from pmo_report.engine import analyze, normalize_status
from pmo_report.models import Project, Task
from pmo_report.parsers import tabular_parser
from pmo_report.parsers.text_parser import _rule_parse

TODAY = date(2026, 8, 24)


def test_ignore_keyword_filters_line():
    """忽略词命中 → 整行跳过（连模糊行也不算）。"""
    text = "下周重点：完成联调并准备上线发布。\n• 结算中心重构 张三 负责，当前进度50%\n接口联调 已完成 进度100%"
    tasks, ambiguous = _rule_parse(text, ignore=["下周重点"])
    names = [t.name for t in tasks]
    assert "下周重点" not in names, names
    assert all("下周重点" not in (t.note or "") for t in tasks)
    assert all("下周重点" not in a for a in ambiguous)
    assert len(tasks) == 2, names
    print("✅ 忽略词过滤")


def test_ignore_keyword_no_effect_when_empty():
    """无忽略词时行为与默认一致（回归保护）。"""
    text = "下周重点：完成联调并准备上线发布。\n• 结算中心重构 张三 负责，当前进度50%"
    tasks, ambiguous = _rule_parse(text)   # 默认无忽略词
    assert "下周重点" not in [t.name for t in tasks]
    assert any("下周重点" in a for a in ambiguous)   # 作为模糊行保留，供 AI 兜底
    print("✅ 无忽略词时模糊行保留（AI 兜底）")


def test_custom_column_alias():
    """自定义列名映射：未内置的表头（完成百分比）映射到 progress。"""
    df = pd.DataFrame({"事项": ["任务A"], "完成百分比": ["60%"]})
    mapping = tabular_parser._map_columns(df, extra_aliases={"完成百分比": "progress"})
    assert mapping.get("progress") == "完成百分比", mapping
    # 不带自定义映射 → 未识别
    mapping2 = tabular_parser._map_columns(df)
    assert "progress" not in mapping2
    # 集成路径：parse_csv 注入规则 → 表头识别 + 列映射 + 忽略词全链路
    import csv as _csv
    p = "/tmp/_rule_map.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        _csv.writer(f).writerows([["事项", "完成百分比"], ["任务X", "80%"], ["占位行", "10%"]])
    proj = tabular_parser.parse_csv(p, custom_rules={"column_aliases": {"完成百分比": "progress"},
                                                     "ignore_keywords": ["占位"]})
    tasks = [(t.name, t.progress) for t in proj.tasks]
    assert ("任务X", 80.0) in tasks, tasks
    assert not any(n == "占位行" for n, _ in tasks), tasks   # 忽略词生效
    print("✅ 自定义列名映射（含集成路径）")


def test_normalize_status_extra():
    """自定义状态词：内置未覆盖的变体经用户映射后归一化。"""
    assert normalize_status("搁置", {"搁置": "已滞后"}) == "已滞后"
    assert normalize_status("搁置") == "搁置"          # 无映射时不改
    assert normalize_status("已上线", {"已上线": "已完成"}) == "已完成"  # 内置优先
    print("✅ 自定义状态词映射")


def test_engine_uses_custom_status_word():
    """引擎集成：规则里带 status_words 时，分析结果使用用户映射。"""
    p = Project(tasks=[Task(name="a", status="搁置")])
    ts = analyze(p, today=TODAY, rules={"status_words": {"搁置": "已滞后"}}).task_stats[0]
    assert ts.status_norm == "已滞后"
    assert ts.risk_level == "关注"   # 已滞后 → 至少关注
    # 不带映射：搁置 无法识别 → 保持原样，默认按未开始处理
    ts2 = analyze(p, today=TODAY).task_stats[0]
    assert ts2.status_norm == "搁置" or ts2.status_norm == "未开始"
    print("✅ 引擎集成自定义状态词")


def test_learn_rules_logic():
    """校对回写核心逻辑：learn 合并去重、目标合法性校验（模拟 app.py 行为）。"""
    from pmo_report import rules as rules_mod
    rules = rules_mod.load_rules()
    rules = dict(rules)
    ignore = rules.setdefault("ignore_keywords", [])
    for n in ["误抓任务A", "误抓任务A", ""]:
        n = n.strip()
        if n and n not in ignore:
            ignore.append(n)
    assert ignore == ["误抓任务A"], ignore   # 去重
    status = rules.setdefault("status_words", {})
    for src, dst in {"搁置": "已滞后", "停摆": "不是标准值"}.items():
        s, d = src.strip(), dst.strip()
        if s and d and d in rules_mod.STATUS_VALUES:
            status[s] = d
    assert status == {"搁置": "已滞后"}   # 非法目标被拒绝
    print("✅ learn 去重与合法性校验")


if __name__ == "__main__":
    test_ignore_keyword_filters_line()
    test_ignore_keyword_no_effect_when_empty()
    test_custom_column_alias()
    test_normalize_status_extra()
    test_engine_uses_custom_status_word()
    test_learn_rules_logic()
    print("\n全部通过 ✅")
