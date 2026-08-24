# -*- coding: utf-8 -*-
"""解析准确率回归测试（黄金数据集对比）。

用法：.venv/bin/python tests/test_golden.py
- 任务召回率：解析出的任务能在黄金集中找到同名任务的比例
- 字段准确率：同名任务中 owner/progress/status/plan_end 等字段一致的比例
- 任一用例召回率 < 0.8 或字段准确率 < 0.7 时退出码非 0

基线生成：.venv/bin/python tests/make_golden.py
"""
import os
import json
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmo_report.parsers import tabular_parser, text_parser

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
GOLDEN_DIR = os.path.join(BASE, "golden")
INPUT_DIR = os.path.join(BASE, "input")

CASES = [
    ("进度表_示例.csv.json", tabular_parser.parse_csv, "进度表_示例.csv"),
    ("样本测试集.csv.json", tabular_parser.parse_csv, "样本测试集.csv"),
    ("周度纪要_示例.docx.json", lambda p: text_parser.parse_docx(p, use_ai=False), "周度纪要_示例.docx"),
]

FIELD_KEYS = ("name", "owner", "progress", "status", "plan_start", "plan_end", "note")


def task_brief(t):
    d = t.to_dict()
    return {k: d.get(k) for k in FIELD_KEYS}


def _norm(v):
    if isinstance(v, float):
        return round(v, 1)
    return v


def evaluate(golden_path, parser, input_name):
    with open(os.path.join(GOLDEN_DIR, golden_path), "r", encoding="utf-8") as f:
        golden = json.load(f)
    proj = parser(os.path.join(INPUT_DIR, input_name))
    got = [task_brief(t) for t in proj.tasks]

    exp_by_name = {t["name"]: t for t in golden["tasks"]}
    matched = 0
    field_ok, field_total = 0, 0
    miss_names = []
    for g in got:
        exp = exp_by_name.get(g["name"])
        if exp is None:
            miss_names.append(g["name"])
            continue
        matched += 1
        for k in FIELD_KEYS:
            if k == "name":
                continue
            if _norm(exp.get(k)) == _norm(g.get(k)):
                field_ok += 1
            field_total += 1
    recall = matched / len(golden["tasks"]) if golden["tasks"] else 1.0
    field_acc = field_ok / field_total if field_total else 1.0
    return {"recall": recall, "field_acc": field_acc, "expected": len(golden["tasks"]),
            "got": len(got), "miss": miss_names}


def main():
    ok_all = True
    print("=" * 72)
    print(f"{'用例':<28}{'期望':>6}{'解析':>6}{'召回率':>9}{'字段准确率':>11}")
    print("-" * 72)
    for golden_path, parser, input_name in CASES:
        r = evaluate(golden_path, parser, input_name)
        flag = "✅" if (r["recall"] >= 0.8 and r["field_acc"] >= 0.7) else "❌"
        print(f"{input_name:<28}{r['expected']:>6}{r['got']:>6}{r['recall']*100:>8.1f}%{r['field_acc']*100:>10.1f}%  {flag}")
        if r["miss"]:
            print(f"   未匹配: {r['miss'][:5]}")
        if r["recall"] < 0.8 or r["field_acc"] < 0.7:
            ok_all = False
    print("=" * 72)
    print("结果:", "全部通过" if ok_all else "存在未达标的用例")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
