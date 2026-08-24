# -*- coding: utf-8 -*-
"""
PM Report Agent — AI 项目管理智能周报助手
将 Excel/CSV/Word/PDF 等混合格式的项目资料，自动提炼为结构化进度，
用规则引擎统计 + AI 生成专业周报。
"""
__version__ = "0.1.0"

from .models import Project, Task
from .engine import analyze, ProjectStats

__all__ = ["Project", "Task", "analyze", "ProjectStats", "__version__"]
