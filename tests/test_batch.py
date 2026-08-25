# -*- coding: utf-8 -*-
"""工作区批量操作接口单元测试。
用法：.venv/bin/python tests/test_batch.py"""
import os
import sys
import tempfile

# 必须在 import app 之前隔离数据目录（避免持久化污染真实库）
os.environ["PM_DATA_DIR"] = tempfile.mkdtemp(prefix="pm_batch_test_")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod
from pmo_report.models import Project


def _reset_workspace():
    app_mod.WORKSPACE["sheets"] = {}
    app_mod.WORKSPACE["groups"] = {}
    for i in range(3):
        sid = "S" + str(i + 1)
        proj = Project(name=f"表{i+1}")
        app_mod.WORKSPACE["sheets"][sid] = {
            "name": proj.name, "source": "x.csv", "source_id": "",
            "project": proj, "_stats": {}, "parse_stats": {}, "rule_tasks": [],
        }


def _make_payload(**kw):
    class P:
        pass
    p = P()
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_batch_move_ungroup_delete():
    _reset_workspace()
    app_mod.WORKSPACE["groups"]["G1"] = {"name": "组A", "sheets": []}

    # 批量移入 G1
    r = app_mod.workspace_batch(_make_payload(action="move", sheet_ids=["S1", "S2", "S9"], gid="G1"))
    assert r["ok"] and sorted(r["done"]) == ["S1", "S2"] and r["missing"] == ["S9"]
    assert app_mod.WORKSPACE["groups"]["G1"]["sheets"] == ["S1", "S2"]

    # 批量移出（回未分组）
    r = app_mod.workspace_batch(_make_payload(action="ungroup", sheet_ids=["S1", "S2"]))
    assert r["ok"] and app_mod.WORKSPACE["groups"]["G1"]["sheets"] == []

    # 批量删除
    r = app_mod.workspace_batch(_make_payload(action="delete", sheet_ids=["S1", "S2", "S3"]))
    assert r["ok"] and len(r["done"]) == 3 and app_mod.WORKSPACE["sheets"] == {}
    print("✅ 批量 move/ungroup/delete")


def test_batch_validation():
    _reset_workspace()
    try:
        app_mod.workspace_batch(_make_payload(action="bad", sheet_ids=["S1"]))
        assert False, "非法 action 应报 400"
    except Exception:
        pass
    try:
        app_mod.workspace_batch(_make_payload(action="move", sheet_ids=["S1"], gid="G_NOPE"))
        assert False, "不存在的分组应报 404"
    except Exception:
        pass
    print("✅ 批量参数校验")


if __name__ == "__main__":
    test_batch_move_ungroup_delete()
    test_batch_validation()
    print("ALL PASS")
