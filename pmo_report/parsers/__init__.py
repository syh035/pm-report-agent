# -*- coding: utf-8 -*-
"""
解析层：把不同格式的输入统一解析为 Project 模型。

支持格式：
  - Excel  (.xlsx/.xlsm)     -> tabular_parser.parse_excel
  - CSV    (.csv)            -> tabular_parser.parse_csv
  - Word   (.docx)           -> text_parser.parse_docx
  - PDF    (.pdf)            -> text_parser.parse_pdf
入口：
  parse_file(path) -> Project
  parse_files(paths) -> 合并多个文件为多个 Project
"""
from __future__ import annotations
import os
from typing import List

from ..models import Project, Task
from . import tabular_parser, text_parser


SUPPORTED_EXTS = {".xlsx", ".xlsm", ".csv", ".docx", ".pdf"}


def parse_file(path: str, project_name: str = "", period: str = "") -> Project:
    """解析单个文件为 Project。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的文件类型: {ext}（支持 {sorted(SUPPORTED_EXTS)}）")

    if ext in (".xlsx", ".xlsm"):
        project = tabular_parser.parse_excel(path)
    elif ext == ".csv":
        project = tabular_parser.parse_csv(path)
    elif ext == ".docx":
        project = text_parser.parse_docx(path, project_name)
    elif ext == ".pdf":
        project = text_parser.parse_pdf(path, project_name)
    else:
        raise ValueError(f"未处理扩展名: {ext}")

    if not project.name:
        project.name = project_name or os.path.splitext(os.path.basename(path))[0]
    if period:
        project.period = period
    project.source_files = [os.path.abspath(path)]
    return project


def parse_files(paths: List[str], period: str = "") -> List[Project]:
    """批量解析多个文件，返回项目列表。同名源文件合并为一个项目。"""
    projects: List[Project] = []
    for p in paths:
        try:
            proj = parse_file(p, period=period)
        except Exception as e:
            print(f"  ⚠️ 跳过 {os.path.basename(p)}: {e}")
            continue
        projects.append(proj)
    return projects
