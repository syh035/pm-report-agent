#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PM Report Agent 命令行入口（可从任意位置执行）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pmo_report.cli import main

if __name__ == "__main__":
    sys.exit(main())
