# -*- coding: utf-8 -*-
"""AI 主链路单元测试：markitdown 转 markdown + JSON 容错解析 + 缓存键参数化。
不调用真实 AI（用 monkeypatch），不依赖网络/Key。
用法：.venv/bin/python tests/test_ai_pipeline.py"""
import os
import sys
import tempfile
import json

os.environ["PM_DATA_DIR"] = tempfile.mkdtemp(prefix="pm_pipeline_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmo_report import ai_pipeline as pipe


def test_json_lenient_truncated():
    """截断的 JSON 应能补全解析。"""
    frag = '{"sections":[{"kind":"task","name":"A","fields":{"x":"1"}},{"kind":"risk","name":"B","fields":{"y":"2"}}'
    d = pipe._parse_json_lenient(frag)
    assert d and len(d["sections"]) == 2
    # 完整 JSON 直接解析
    d2 = pipe._parse_json_lenient('{"sections":[],"summary":"ok"}')
    assert d2 and d2["summary"] == "ok"
    # 非 JSON 返回 None
    assert pipe._parse_json_lenient("随便说点什么") is None
    print("✅ JSON 容错解析")


def test_doc_to_markdown_text():
    """纯文本/HTML 直接读取（不走 markitdown 子进程）。"""
    p = os.path.join(tempfile.gettempdir(), "_pipe_test.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 测试\n任务A 60%")
    md = pipe.doc_to_markdown(p)
    assert "任务A" in md
    print("✅ 文本转 markdown")


def test_cache_key_includes_params():
    """缓存键应包含 max_tokens/temperature（调整参数不命中旧缓存）。"""
    from pmo_report import ai as ai_mod
    k1 = ai_mod._cache_key("s", "m", [{"role": "user", "content": "hi"}], "", max_tokens=500)
    k2 = ai_mod._cache_key("s", "m", [{"role": "user", "content": "hi"}], "", max_tokens=8000)
    k3 = ai_mod._cache_key("s", "m", [{"role": "user", "content": "hi"}], "", max_tokens=8000)
    assert k1 != k2, "max_tokens 不同应产生不同缓存键"
    assert k2 == k3, "相同参数应命中相同缓存键"
    print("✅ 缓存键参数化")


if __name__ == "__main__":
    test_json_lenient_truncated()
    test_doc_to_markdown_text()
    test_cache_key_includes_params()
    print("\n全部通过 ✅")
