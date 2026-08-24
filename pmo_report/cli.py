# -*- coding: utf-8 -*-
"""命令行入口：pm-report-agent"""

from __future__ import annotations
import argparse
import os
import sys


def _cmd_report(args):
    from .parsers import parse_files
    from .engine import analyze
    from .report import ReportGenerator

    paths = args.input
    if not paths:
        print("请提供输入文件（Excel/CSV/Word/PDF）")
        sys.exit(1)

    # 可选：从目录读取所有支持的文件
    all_paths = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in files:
                    if f.lower().endswith((".xlsx",".xlsm",".csv",".docx",".pdf")) and not f.startswith("~$"):
                        all_paths.append(os.path.join(root, f))
        else:
            all_paths.append(p)

    print(f"读取 {len(all_paths)} 个文件 ...")
    projects = parse_files(all_paths, period=args.period)

    gen = ReportGenerator()
    if args.template:
        # 用户提供模板文件并保存为自定义模板（后续延用）
        with open(args.template, "r", encoding="utf-8") as f:
            content = f.read()
        saved = gen.save_custom_template(content)
        print(f"已加载并保存自定义模板：{saved}")

    from .rules import load_rules
    current_rules = load_rules()
    for proj in projects:
        stats = analyze(proj, rules=current_rules)
        result = gen.render(stats, use_ai=not args.no_ai)
        print("\n" + "="*70)
        print(result["report"])
        print("="*70)
        if result.get("error"):
            print(f"\n[提示] AI 未生效（{result['error']}），已使用规则引擎回退文案。")
        elif not result.get("ai_used"):
            print("\n[提示] 本次使用规则引擎文案（可按需配置 DeepSeek API Key 启用 AI 增强）。")
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(result["report"])
            print(f"\n报告已保存：{args.output}")


def _cmd_template(args):
    from .report import ReportGenerator
    gen = ReportGenerator()
    if args.show:
        tpl = gen.load_custom_template() or ""
        print(tpl if tpl else "（当前使用默认模板，尚无自定义模板）")
    elif args.edit:
        # 打开编辑器编辑自定义模板
        content = gen.load_custom_template() or ReportGenerator.DEFAULT_TEMPLATE
        tmp = os.path.join(args.edit, "custom_report.md") if os.path.isdir(args.edit) else args.edit
        print(f"请编辑模板文件后保存：{tmp}")
        # 简单起见：提示用户
        print("当前模板内容：\n" + content)
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            gen.save_custom_template(f.read())
        print(f"已保存自定义模板：{ReportGenerator.CUSTOM_TEMPLATE_FILE}")
    elif args.reset:
        gen.reset_template()
        print("已恢复默认模板")
    else:
        print("用法：--show / --file 模板.md / --reset")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm-report-agent", description="AI 项目管理智能周报助手")
    sub = parser.add_subparsers(dest="command")

    # report 子命令
    p_report = sub.add_parser("report", help="生成周报")
    p_report.add_argument("input", nargs="*", help="输入文件或目录（Excel/CSV/Word/PDF）")
    p_report.add_argument("--period", default="", help="统计周期，如 2026-W33")
    p_report.add_argument("--template", help="自定义周报模板文件路径（保存后延用）")
    p_report.add_argument("-o", "--output", help="输出周报到文件")
    p_report.add_argument("--no-ai", action="store_true", help="不使用 AI 生成（仅规则引擎）")

    # template 子命令
    p_tpl = sub.add_parser("template", help="管理周报模板")
    p_tpl.add_argument("--show", action="store_true", help="显示当前模板")
    p_tpl.add_argument("--file", help="从文件导入模板")
    p_tpl.add_argument("--edit", nargs="?", const="./", help="编辑模板文件（参数为目录/文件，默认当前目录）")
    p_tpl.add_argument("--reset", action="store_true", help="恢复默认模板")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "report":
        _cmd_report(args)
    elif args.command == "template":
        _cmd_template(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
