# -*- coding: utf-8 -*-
"""生成黄金数据集：把当前解析器的确定性输出存为基线 JSON。
用法：.venv/bin/python tests/make_golden.py
之后改解析器时，跑 tests/test_golden.py 看准确率变化。"""
import os
import json
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pmo_report.parsers import tabular_parser, text_parser

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
GOLDEN_DIR = os.path.join(BASE, "golden")
INPUT_DIR = os.path.join(BASE, "input")

CASES = [
    # (输入文件, 解析函数, golden 文件名)
    ("进度表_示例.csv", tabular_parser.parse_csv, "进度表_示例.csv.json"),
    ("样本测试集.csv", tabular_parser.parse_csv, "样本测试集.csv.json"),
    ("周度纪要_示例.docx", lambda p: text_parser.parse_docx(p, use_ai=False), "周度纪要_示例.docx.json"),
    # 脏数据用例（对抗性输入）：乱列名/多种进度与日期写法/缺失字段/GBK 编码/叙述干扰行
    ("脏数据_混合格式.csv", tabular_parser.parse_csv, "脏数据_混合格式.csv.json"),
    ("脏数据_GBK.csv", tabular_parser.parse_csv, "脏数据_GBK.csv.json"),
    ("脏文本_示例.docx", lambda p: text_parser.parse_docx(p, use_ai=False), "脏文本_示例.docx.json"),
]


def task_brief(t):
    """抽取用于对比的关键字段（排除运行期字段）。"""
    d = t.to_dict()
    return {k: d.get(k) for k in ("name", "owner", "progress", "status", "plan_start", "plan_end", "note")}


def main():
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    for fname, parser, golden in CASES:
        path = os.path.join(INPUT_DIR, fname)
        proj = parser(path)
        data = {
            "file": f"examples/input/{fname}",
            "tasks": [task_brief(t) for t in proj.tasks],
        }
        out = os.path.join(GOLDEN_DIR, golden)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已生成 {golden}  ({len(proj.tasks)} 条任务)")


if __name__ == "__main__":
    main()
