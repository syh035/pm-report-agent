# -*- coding: utf-8 -*-
"""AI 成本优化单元测试：缓存命中 / Token 用量记录 / 候选行预筛 / 空文本回退。
用法：.venv/bin/python tests/test_ai_optimize.py（不依赖网络，monkeypatch 底层调用）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pmo_report.ai as ai


def test_cache_and_usage():
    """相同输入第二次应命中缓存：真实调用 1 次、用量只记 1 次。"""
    ai.clear_usage()
    ai._write_json(ai.CACHE_FILE, {})   # 清缓存，保证可重复运行
    calls = {"n": 0}

    def fake_call(messages, **kw):
        calls["n"] += 1
        ai._record_usage(kw.get("site", "ai"), 100, 50)   # 模拟 API 返回的 usage
        return "[综述]进展顺利，完成率 0%。[计划]持续推进。"

    ai.call_deepseek = fake_call
    msgs = [{"role": "user", "content": "同一份数据"}]
    r1 = ai.call_with_cache("report", msgs, temperature=0.6, max_tokens=400)
    r2 = ai.call_with_cache("report", msgs, temperature=0.6, max_tokens=400)
    assert r1 == r2, "两次结果应一致"
    assert calls["n"] == 1, f"第二次应命中缓存，实际调用 {calls['n']} 次"
    s = ai.usage_summary()
    assert s["calls"] == 1 and s["cache_hits"] == 1, f"calls={s['calls']} cache_hits={s['cache_hits']}"
    assert s["total_in"] == 100 and s["total_out"] == 50
    print("✅ 缓存命中：真实调用 1 次，缓存命中 1 次，Token 只计一次")


def test_cache_different_input():
    """不同输入不命中缓存。"""
    ai._write_json(ai.CACHE_FILE, {})
    calls = {"n": 0}

    def fake_call(messages, **kw):
        calls["n"] += 1
        ai._record_usage(kw.get("site", "ai"), 10, 5)
        return "ok"

    ai.call_deepseek = fake_call
    ai.call_with_cache("report", [{"role": "user", "content": "A"}], temperature=0.3)
    ai.call_with_cache("report", [{"role": "user", "content": "B"}], temperature=0.3)
    assert calls["n"] == 2, "不同输入应各调用一次"
    print("✅ 不同输入不命中缓存")


def test_candidate_text():
    """规则预筛候选行：只保留命中的行，长度大幅缩短。"""
    from pmo_report.parsers.text_parser import _candidate_text
    from pmo_report.models import Task
    tasks = [Task(name="a", note="财务模块开发 张三负责 进度60%"),
             Task(name="b", note=""),
             Task(name="c", note="财务模块开发 张三负责 进度60%")]  # 重复行去重
    cand = _candidate_text(tasks)
    assert "财务模块开发" in cand and "进度60%" in cand
    assert cand.count("财务模块开发") == 1, "重复候选行应去重"
    assert len(cand) < 100
    print("✅ 候选行预筛：去重 + 大幅缩短输入")


def test_enrich_fallback():
    """空文本直接回退，不发请求。"""
    from pmo_report.models import Task
    tasks = [Task(name="x")]
    out = ai.enrich_tasks_from_text("   ", tasks)
    assert out is tasks
    print("✅ 空文本回退：不调用 AI")


if __name__ == "__main__":
    test_cache_and_usage()
    test_cache_different_input()
    test_candidate_text()
    test_enrich_fallback()
    print("\n全部通过 ✅")
