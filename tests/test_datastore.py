# -*- coding: utf-8 -*-
"""分类数据仓 + 源数据仓库单元测试（PM_DATA_DIR 指向临时目录，不污染真实数据）。
用法：.venv/bin/python tests/test_datastore.py"""
import os
import sys
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="pm_datastore_test_")
os.environ["PM_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmo_report import datastore


def test_source_roundtrip():
    src = os.path.join(_TMP, "demo.csv")
    with open(src, "w", encoding="utf-8") as f:
        f.write("任务,进度\nA,60\nB,100\n")
    sid = datastore.save_source("demo.csv", src)
    info = datastore.get_source(sid)
    assert info and info["filename"] == "demo.csv"
    assert os.path.exists(info["stored_path"]), "原始文件应永久保留"
    assert any(s["id"] == sid for s in datastore.list_sources())
    print("✅ 源文件归档与查询")


def test_items_classify():
    src = os.path.join(_TMP, "demo2.csv")
    with open(src, "w", encoding="utf-8") as f:
        f.write("任务\nX\n")
    sid = datastore.save_source("demo2.csv", src)
    n = datastore.add_items(sid, [
        ("task", "支付网关", {"progress": 60, "source_line": "第1行"}, "2026-W35"),
        ("task", "登录模块", {"progress": 100}, "2026-W35"),
        ("risk", "支付网关", {"level": "风险", "reason": "超期"}, "2026-W35"),
        ("decision", "技术选型", {"note": "选用A方案"}, "2026-W34"),
        ("raw", "未分类文本", {}, ""),
        ("bad", "非法分类", {}, ""),   # 非法分类应被跳过
    ])
    assert n == 5
    tasks = datastore.query_items(kind="task")
    assert len(tasks) == 2 and all(t["kind"] == "task" for t in tasks)
    risks = datastore.query_items(kind="risk")
    assert len(risks) == 1 and risks[0]["payload"]["level"] == "风险"
    by_period = datastore.query_items(period="2026-W35")
    assert len(by_period) == 3
    assert datastore.items_summary().get("task") == 2
    # 删除源 → 关联条目一并删除
    datastore.delete_source(sid)
    assert datastore.query_items(kind="task", source_id=sid) == []
    print("✅ 分类入仓/查询/删除级联")


if __name__ == "__main__":
    test_source_roundtrip()
    test_items_classify()
    shutil.rmtree(_TMP, ignore_errors=True)
    print("\n全部通过 ✅")
