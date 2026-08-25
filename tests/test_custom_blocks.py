# -*- coding: utf-8 -*-
"""自定义块（AI 提示词块 + 公式计算块 + 库保存）单元测试。
用法：.venv/bin/python tests/test_custom_blocks.py（AI 调用用 monkeypatch，不依赖网络）"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmo_report.report import _eval_formula, _block_formula, _block_ai
from pmo_report import custom_blocks as cb
from pmo_report.engine import analyze
from pmo_report.models import Project, Task

TODAY = date(2026, 8, 24)


def _stats():
    p = Project(tasks=[Task(name="A", progress=100), Task(name="B", progress=0),
                       Task(name="C", progress=0, status="已滞后"),
                       Task(name="D", progress=60)])
    return analyze(p, today=TODAY)


def test_formula_eval():
    s = _stats()   # 4 任务：A 完成，B/C/D 未完成
    assert _eval_formula("completion_rate", s) == 25.0
    assert abs(_eval_formula("risk/total*100", s) - 25.0) < 1e-6   # C 已滞后 → 风险1/4
    assert _eval_formula("avg_progress - avg_target_progress", s) is not None
    assert _eval_formula("(done + in_progress) / total * 100", s) == 50.0
    try:
        _eval_formula("__import__('os').system('x')", s)
        raise AssertionError("非法表达式应被拒绝")
    except ValueError:
        pass
    try:
        _eval_formula("1/0", s)
        raise AssertionError("除零应抛错")
    except ZeroDivisionError:
        pass
    print("✅ 公式安全求值")


def test_formula_block():
    s = _stats()
    ctx = {"stats": s, "rules": {}}
    html, text = _block_formula({"type": "formula", "expression": "completion_rate",
                                 "label": "完成率", "as_percent": True}, ctx, None)
    assert "25.0%" in html and "完成率" in html
    html2, _ = _block_formula({"type": "formula", "expression": "1/0", "label": "x"}, ctx, None)
    assert "公式块错误" in html2
    print("✅ 公式块渲染")


def test_ai_block_render():
    import pmo_report.ai as ai
    orig = ai.call_with_cache
    ai._write_json(ai.CACHE_FILE, {})
    ai.call_with_cache = lambda site, messages, **kw: "管理层最应关注：完成率与风险。"
    try:
        s = _stats()
        ctx = {"stats": s, "projects": [], "delta_rows": []}
        html, text = _block_ai({"type": "ai_block", "data_scope": "stats_summary",
                                "prompt": "从 {data} 中总结。", "system": ""}, ctx, None)
        assert "管理层最应关注" in html and "完成率与风险" in text
        # 数据源组装不应为空
        assert _block_ai({"type": "ai_block", "data_scope": "risks", "prompt": "p {data}"}, ctx, None)[0]
    finally:
        ai.call_with_cache = orig
    print("✅ AI 块渲染（monkeypatch）")


def test_custom_blocks_library():
    name = "管理层关注"
    try:
        blocks = cb.save_block(name, {"type": "ai_block", "data_scope": "risks", "prompt": "总结 {data}"})
        assert name in blocks and blocks[name]["type"] == "ai_block"
        blocks = cb.save_block("风险占比", {"type": "formula", "expression": "risk/total*100", "label": "风险占比"})
        assert "风险占比" in blocks
        assert len(cb.load_blocks()) >= 2
        blocks = cb.delete_block(name)
        assert name not in blocks and "风险占比" in blocks
    finally:
        cb.delete_block("风险占比")
        cb.delete_block(name)
    print("✅ 自定义块库保存/删除")


if __name__ == "__main__":
    test_formula_eval()
    test_formula_block()
    test_ai_block_render()
    test_custom_blocks_library()
    print("\n全部通过 ✅")
