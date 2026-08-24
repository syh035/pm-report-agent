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


def test_enrich_schema_validation():
    """AI 提炼 schema 校验：非法条目被拒绝/钳制，不污染统计。"""
    from pmo_report.models import Task
    ai._write_json(ai.CACHE_FILE, {})

    def fake_call(messages, **kw):
        ai._record_usage(kw.get("site", "ai"), 10, 5)
        return ('[{"name":"好任务","progress":50,"status":"进行中"},'
                '{"name":"","progress":120},'
                '{"name":"坏进度","progress":999},'
                '{"name":"坏状态","progress":0.5,"status":"随便写"}]')

    ai.call_deepseek = fake_call
    tasks = ai.enrich_tasks_from_text("候选行", [Task(name="好任务")])
    by_name = {t.name: t for t in tasks}
    assert "好任务" in by_name, list(by_name)
    assert by_name["好任务"].progress == 50.0
    assert by_name["好任务"].status == "进行中"
    # 进度 999 超界 → 钳制为 None；0.5 → 50；状态非法 → 置空
    assert by_name["坏进度"].progress is None
    assert by_name["坏状态"].progress == 50.0
    assert by_name["坏状态"].status == ""
    assert "" not in by_name, "空任务名应被拒绝"
    print("✅ 提炼 schema 校验")


def test_ai_config_defaults():
    """AI 配置默认值（模型/接口），不依赖外部状态。"""
    import importlib
    orig = dict(ai._read_keys_file())
    try:
        ai._write_json(ai.KEYS_FILE, {"deepseek_api_key": "sk-placeholder"})
        assert ai.get_model() == "deepseek-chat"
        assert ai.get_base_url() == "https://api.deepseek.com"
        # 保存自定义配置后生效
        ai.save_ai_config("deepseek-reasoner", "https://api.deepseek.com")
        assert ai.get_model() == "deepseek-reasoner"
        # 非法地址回落默认
        ai.save_ai_config("deepseek-chat", "not-a-url")
        assert ai.get_base_url() == "https://api.deepseek.com"
        print("✅ AI 模型/接口配置默认值与校验")
    finally:
        ai._write_json(ai.KEYS_FILE, orig)


if __name__ == "__main__":
    test_cache_and_usage()
    test_cache_different_input()
    test_candidate_text()
    test_enrich_fallback()
    test_enrich_schema_validation()
    test_ai_config_defaults()
    print("\n全部通过 ✅")
