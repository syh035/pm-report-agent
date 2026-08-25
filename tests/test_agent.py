# -*- coding: utf-8 -*-
"""Agent 一句话入口（受限 agent）单元测试：意图识别关键词回退 + 动作执行。
用法：.venv/bin/python tests/test_agent.py（AI 调用用 monkeypatch，不依赖网络/Key）"""
import os
import sys
import tempfile

# 隔离数据目录：import app 时 _restore_workspace 只读临时库，避免测试间共享真实状态
os.environ["PM_DATA_DIR"] = tempfile.mkdtemp(prefix="pm_agent_test_")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod
import pmo_report.ai as ai


def _fallback(instruction):
    """模拟无 Key：call_with_cache 抛错 → 走关键词回退。"""
    orig = ai.call_with_cache
    ai.call_with_cache = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("未配置 Key"))
    try:
        intent, params, ai_used = app_mod._parse_agent_intent(instruction, [])
        assert ai_used is False, "无 Key 应走规则回退"
        return intent, params
    finally:
        ai.call_with_cache = orig


def test_intent_fallback():
    assert _fallback("帮我生成一份周报")[0] == "generate_report"
    intent, params = _fallback("生成日报 项目：支付项目")
    assert intent == "generate_report" and params.get("report_type") == "day"
    assert params.get("project_name") == "支付项目"
    assert _fallback("列出当前风险")[0] == "risks"
    assert _fallback("看板概览")[0] == "dashboard"
    assert _fallback("你好")[0] == "help"
    print("✅ 意图识别关键词回退")


def test_agent_execute_help_and_risks():
    log = []
    r = app_mod._agent_help(log)
    assert "生成周报" in r["text"]
    r2 = app_mod._agent_risks(log)
    assert isinstance(r2["report"], str)
    print("✅ Agent 帮助/风险动作执行")


if __name__ == "__main__":
    test_intent_fallback()
    test_agent_execute_help_and_risks()
    print("\n全部通过 ✅")
