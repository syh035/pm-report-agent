# -*- coding: utf-8 -*-
"""
周报生成器：把规则统计结果 + AI 生成内容，按 HTML 模板渲染成周报。

模板系统：
  - 默认模板为 HTML（富文本，无 markdown 符号）
  - 占位符：{project_name} {period} {today} {overview} {stats_html}
            {status_html} {risks_html} {next_plan}
  - 用户可自定义模板（存 templates/custom_report.html），保存后自动延用
  - render() 返回 report_html（页面展示）+ report_text（导出/纯文本）
"""
from __future__ import annotations
import os
import json
import re
from datetime import date
from typing import Dict, Optional

from .engine import ProjectStats
from . import ai as ai_module
from .models import STATUS_DONE


TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
CUSTOM_TEMPLATE_FILE = os.path.join(TEMPLATE_DIR, "custom_report.html")
CUSTOM_BLOCKS_FILE = os.path.join(TEMPLATE_DIR, "custom_template.json")

# 默认模板 blocks（JSON 模块化，优先）
DEFAULT_BLOCKS_TEMPLATE = None  # 延迟导入，避免循环


# 默认 HTML 模板（无 markdown 符号，富文本）
DEFAULT_TEMPLATE = """<h1>{project_name} 项目周报</h1>
<p class="meta">周期：{period} ｜ 生成日期：{today}</p>

<h2>一、本周总体进展</h2>
<p>{overview}</p>

<h2>二、核心数据</h2>
{stats_html}

<h2>三、完成情况</h2>
<p>{status_html}</p>

<h2>四、风险与需关注事项</h2>
{risks_html}

<h2>五、下周计划</h2>
<p>{next_plan}</p>

{delta_html}
"""

# 默认纯文本版（导出为 .txt 用，同样去 markdown）
DEFAULT_TEMPLATE_TEXT = """{project_name} 项目周报
周期：{period} ｜ 生成日期：{today}

一、本周总体进展
{overview}

二、核心数据
{stats_text}

三、完成情况
{status_text}

四、风险与需关注事项
{risks_text}

五、下周计划
{next_plan}

{delta_text}
"""


class ReportGenerator:
    # 类属性别名，兼容 app.py / cli.py 的类引用写法
    DEFAULT_TEMPLATE = DEFAULT_TEMPLATE

    def __init__(self, template: Optional[str] = None):
        self.template = (template or self.load_custom_template() or DEFAULT_TEMPLATE)

    # ---- 模板持久化 ----
    @staticmethod
    def load_custom_template() -> Optional[str]:
        # 优先 blocks JSON 模板，其次 HTML
        blocks = ReportGenerator.load_blocks_template()
        if blocks is not None:
            return blocks
        if os.path.exists(CUSTOM_TEMPLATE_FILE):
            try:
                with open(CUSTOM_TEMPLATE_FILE, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                return None
        return None

    @staticmethod
    def load_blocks_template():
        """加载 blocks JSON 模板；无则返回 None。"""
        if os.path.exists(CUSTOM_BLOCKS_FILE):
            try:
                import json
                with open(CUSTOM_BLOCKS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else None
            except Exception:
                return None
        return None

    @staticmethod
    def save_blocks_template(blocks) -> str:
        """保存 blocks 模板为 JSON，同时删除旧的 HTML 模板避免冲突。"""
        import json
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        with open(CUSTOM_BLOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump(blocks, f, ensure_ascii=False, indent=2)
        if os.path.exists(CUSTOM_TEMPLATE_FILE):
            os.remove(CUSTOM_TEMPLATE_FILE)
        return CUSTOM_BLOCKS_FILE

    @staticmethod
    def save_custom_template(content: str) -> str:
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        with open(CUSTOM_TEMPLATE_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        if os.path.exists(CUSTOM_BLOCKS_FILE):
            os.remove(CUSTOM_BLOCKS_FILE)
        return CUSTOM_TEMPLATE_FILE

    @staticmethod
    def reset_template() -> None:
        if os.path.exists(CUSTOM_TEMPLATE_FILE):
            os.remove(CUSTOM_TEMPLATE_FILE)
        if os.path.exists(CUSTOM_BLOCKS_FILE):
            os.remove(CUSTOM_BLOCKS_FILE)

    # ---- HTML 渲染（规则层） ----
    def _stats_html(self, stats: ProjectStats, rules: Dict = None) -> str:
        """核心数据 HTML 表格。可用规则颜色高亮风险/偏慢。"""
        risk_c = (rules or {}).get("color_risk", "#C0504D")
        warn_c = (rules or {}).get("color_warning", "#E65100")
        rows = [
            ("总任务数", str(stats.total_tasks)),
            ("已完成", str(stats.done_count)),
            ("进行中", str(stats.in_progress_count)),
            ("未开始", str(stats.not_started_count)),
            ("已滞后", str(stats.delayed_count)),
            ("完成率", f"{stats.completion_rate}%"),
            ("平均目标进度", f"{stats.avg_target_progress:.0f}%" if stats.avg_target_progress is not None else "-"),
            ("平均实际进度", f"{stats.avg_progress:.0f}%"),
        ]
        html = ['<table class="kpi">']
        for k, v in rows:
            html.append(f"<tr><td>{k}</td><td>{v}</td></tr>")
        html.append("</table>")
        return "\n".join(html)

    def _status_html(self, stats: ProjectStats) -> str:
        done = [ts.task.name for ts in stats.task_stats if (ts.task.progress or 0) >= 100]
        doing = [ts.task.name for ts in stats.task_stats if 0 < (ts.task.progress or 0) < 100]
        pending = [ts.task.name for ts in stats.task_stats if (ts.task.progress or 0) <= 0]
        parts = []
        if done:
            parts.append("已完成：" + "、".join(done))
        if doing:
            parts.append("进行中：" + "、".join(doing))
        if pending:
            parts.append("未开始：" + "、".join(pending))
        html = parts and "<br>\n".join(parts) or "本周无进行中任务。"
        return html

    def _risks_html(self, stats: ProjectStats, rules: Dict = None) -> str:
        risk_c = (rules or {}).get("color_risk", "#C0504D")
        warn_c = (rules or {}).get("color_warning", "#E65100")
        risks = [ts for ts in stats.task_stats if ts.risk_level != "正常"]
        if not risks:
            return "本周无重大风险项，项目整体受控。"
        items = []
        for ts in risks:
            level = ts.risk_level
            color = risk_c if level == "风险" else warn_c
            reason = ts.risk_reason or "需关注"
            items.append(
                f'<li><span class="pill" style="background:{color};color:#fff;border-radius:10px;'
                f'padding:2px 10px;">{level}</span> <b>{ts.task.name}</b>：{reason}</li>'
            )
        return '<ul style="margin-left:18px;line-height:1.8;">' + "\n".join(items) + "</ul>"

    # ---- 纯文本渲染（导出用） ----
    def _stats_text(self, stats: ProjectStats) -> str:
        lines = []
        rows = [
            ("总任务数", str(stats.total_tasks)),
            ("已完成", str(stats.done_count)),
            ("进行中", str(stats.in_progress_count)),
            ("未开始", str(stats.not_started_count)),
            ("已滞后", str(stats.delayed_count)),
            ("完成率", f"{stats.completion_rate}%"),
            ("平均目标进度", f"{stats.avg_target_progress:.0f}%" if stats.avg_target_progress is not None else "-"),
            ("平均实际进度", f"{stats.avg_progress:.0f}%"),
        ]
        for k, v in rows:
            lines.append(f"{k}：{v}")
        return "\n".join(lines)

    def _status_text(self, stats: ProjectStats) -> str:
        done = [ts.task.name for ts in stats.task_stats if (ts.task.progress or 0) >= 100]
        doing = [ts.task.name for ts in stats.task_stats if 0 < (ts.task.progress or 0) < 100]
        pending = [ts.task.name for ts in stats.task_stats if (ts.task.progress or 0) <= 0]
        parts = []
        if done:
            parts.append("已完成：" + "、".join(done))
        if doing:
            parts.append("进行中：" + "、".join(doing))
        if pending:
            parts.append("未开始：" + "、".join(pending))
        return parts and "\n".join(parts) or "本周无进行中任务。"

    def _risks_text(self, stats: ProjectStats) -> str:
        risks = [ts for ts in stats.task_stats if ts.risk_level != "正常"]
        if not risks:
            return "本周无重大风险项，项目整体受控。"
        lines = []
        for ts in risks:
            reason = ts.risk_reason or "需关注"
            lines.append(f"{ts.task.name}（{ts.risk_level}）：{reason}")
        return "\n".join(lines)

    # ---- 环比（与上周对比） ----
    @staticmethod
    def _delta_parts(stats: ProjectStats, prev: Optional[Dict]) -> List[str]:
        """生成环比文案行；prev 为上一期的 stats to_dict，缺省返回空列表。"""
        if not prev:
            return []
        rows = []
        d = lambda cur, old: "" if old is None or cur is None else f"（{'+' if round(cur - old, 1) > 0 else ''}{round(cur - old, 1)}）"
        if prev.get("completion_rate") is not None:
            rows.append(f"完成率 {prev['completion_rate']:.1f}% → {stats.completion_rate:.1f}%{d(stats.completion_rate, prev['completion_rate'])}")
        if prev.get("avg_progress") is not None:
            rows.append(f"平均实际进度 {prev['avg_progress']:.1f}% → {stats.avg_progress:.1f}%{d(stats.avg_progress, prev['avg_progress'])}")
        if prev.get("risk") is not None:
            rows.append(f"风险项 {prev['risk']} → {stats.risk_count}{d(stats.risk_count, prev['risk'])}")
        if prev.get("delayed") is not None:
            rows.append(f"滞后任务 {prev['delayed']} → {stats.delayed_count}{d(stats.delayed_count, prev['delayed'])}")
        if prev.get("total") is not None and prev["total"] != stats.total_tasks:
            rows.append(f"任务总数 {prev['total']} → {stats.total_tasks}")
        # 环比增强：只对比两周都存在的任务集合（剔除新增/删除影响）
        or_ = prev.get("overlap_rate")
        if or_:
            rows.append(f"同任务集合完成率 {or_['prev']:.1f}% → {or_['cur']:.1f}%（{or_['n']} 项）")
        # 环比增强：连续两周无进展
        np_ = prev.get("no_progress")
        if np_:
            shown = "、".join(np_[:6]) + (" 等" if len(np_) > 6 else "")
            rows.append(f"连续两周无进展：{shown}（{len(np_)} 项）")
        return rows

    def _delta_section(self, stats: ProjectStats, prev: Optional[Dict], for_text: bool = False) -> str:
        """返回整段环比章节（含标题）；无上一期数据时返回空串。"""
        rows = self._delta_parts(stats, prev)
        if not rows:
            return ""
        if for_text:
            return "六、与上周对比\n" + "\n".join(rows)
        lis = "".join(f"<li>{r}</li>" for r in rows)
        return ('<h2>六、与上周对比</h2>\n'
                '<ul style="margin:10px 0 10px 18px;line-height:1.8;">' + lis + "</ul>")

    # ---- AI 增强 ----
    def _ai_generate(self, stats: ProjectStats) -> Dict[str, str]:
        """生成 overview / next_plan（纯文本段，无 markdown 符号）。
        载荷瘦身：只发汇总数字 + 截断任务清单（省 Token）；
        走磁盘缓存：相同数据不重复付费；
        产出后做「数字校正」：AI 文本中的百分比若与统计不符，自动改为真实值，防止编造。"""
        project_name = stats.project.name or "项目"
        risks = [{"name": ts.task.name, "level": ts.risk_level, "reason": ts.risk_reason}
                 for ts in stats.task_stats if ts.risk_level != "正常"]
        inprog = [{"name": ts.task.name, "progress": ts.task.progress}
                  for ts in stats.task_stats if 0 < (ts.task.progress or 0) < 100]
        data = {
            "project": project_name,
            "period": stats.project.period or "本期",
            "completion_rate": stats.completion_rate,
            "avg_progress": stats.avg_progress,
            "avg_target_progress": stats.avg_target_progress,
            "total": stats.total_tasks,
            "done": stats.done_count,
            "risk_items": risks[:10],                 # 截断，控制输入长度
            "tasks_in_progress": inprog[:10],
            "risk_truncated": len(risks) > 10,
            "tasks_truncated": len(inprog) > 10,
        }
        prompt = (
            "你是资深的 PMO 项目管理人员，负责撰写项目周报。以下是一个项目的结构性数据：\n"
            f"{json.dumps(data, ensure_ascii=False)}\n\n"
            "请输出两段纯文字（不要用任何 markdown 符号、不要用 # 或 * 或列表符号）：\n"
            "第一段标题：进展综述 —— 3-5 句话的本周进展概述，结论先行，突出完成情况和推进节奏。\n"
            "第二段标题：下周计划 —— 2-4 句，给出下周工作重点建议。\n"
            "严格约束：文中出现的所有百分比和任务数字，只能来自上面给定的数据，禁止编造任何数字；"
            "提到任务名只能用数据中的任务名。\n"
            "用以下格式分隔两段：\n"
            "[综述]...内容...\n[计划]...内容..."
        )
        try:
            text = ai_module.call_with_cache(
                "report",
                [
                    {"role": "system", "content": "你是严谨、专业的 PMO 项目周报撰写专家，只输出纯文本，数字一律引用给定数据。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.6,
                max_tokens=400,
            )
            overview, next_plan = self._split_ai_output(text)
            overview, n1 = self._correct_ai_numbers(overview, stats)
            next_plan, n2 = self._correct_ai_numbers(next_plan, stats)
            return {"overview": overview, "next_plan": next_plan, "numbers_corrected": n1 + n2}
        except Exception as e:
            return {
                "overview": self._fallback_overview(stats),
                "next_plan": self._fallback_next(stats),
                "ai_error": str(e),
            }

    @staticmethod
    def _correct_ai_numbers(text: str, stats: ProjectStats):
        """校正 AI 文本中的数字：与统计不符的百分比自动替换为真实值。
        规则：附近有关键词（完成率/平均进度等）→ 用对应指标；否则取最接近的指标。
        返回 (修正后文本, 修正处数)。"""
        if not text:
            return text, 0
        allowed = {
            "completion_rate": stats.completion_rate,
            "avg_progress": stats.avg_progress,
            "avg_target_progress": stats.avg_target_progress,
        }
        kw_map = [
            ("完成率", "completion_rate"),
            ("平均目标", "avg_target_progress"),
            ("目标进度", "avg_target_progress"),
            ("平均进度", "avg_progress"),
            ("平均实际", "avg_progress"),
        ]

        def nearest(val: float):
            best, bd = None, 1e9
            for v in allowed.values():
                if v is None:
                    continue
                d = abs(val - v)
                if d < bd:
                    bd, best = d, v
            return best

        pat = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
        out, last, corrected = [], 0, 0
        for m in pat.finditer(text):
            val = float(m.group(1))
            if any(v is not None and abs(val - v) <= 1.5 for v in allowed.values()):
                out.append(text[last:m.end()])
                last = m.end()
                continue
            # 取数字前最近的关键词，避免被更早句子的关键词干扰
            ctx = text[max(0, m.start() - 10):m.start()]
            key, best_pos = None, -1
            for kw, k in kw_map:
                pos = ctx.rfind(kw)
                if pos > best_pos:
                    best_pos, key = pos, k
            rep = allowed.get(key) if key else nearest(val)
            if rep is None:
                out.append(text[last:m.end()])
                last = m.end()
                continue
            corrected += 1
            out.append(text[last:m.start()])
            out.append(f"{rep:.0f}%")
            last = m.end()
        out.append(text[last:])
        return "".join(out), corrected

    @staticmethod
    def _split_ai_output(text: str):
        overview = next_plan = ""
        m1 = re.search(r"\[综述\](.*?)(?:\[计划\]|$)", text, re.S)
        m2 = re.search(r"\[计划\](.*?)$", text, re.S)
        if m1:
            overview = m1.group(1).strip()
        if m2:
            next_plan = m2.group(1).strip()
        if not overview:
            overview = text.strip()
        # 清理残留 markdown 符号
        overview = re.sub(r"[#*`>]", "", overview).strip()
        next_plan = re.sub(r"[#*`>]", "", next_plan).strip()
        return overview or "本周项目整体推进，详见数据。", next_plan or "下周推动进行中任务收口并控制风险。"

    def _fallback_overview(self, stats):
        return (
            f"本周项目整体完成率为 {stats.completion_rate}%，平均实际进度 {stats.avg_progress:.0f}%。"
            f"累计完成 {stats.done_count} 项任务，其中进行中 {stats.in_progress_count} 项、"
            f"未开始 {stats.not_started_count} 项。项目整体处于{'正常推进' if stats.risk_count == 0 else '需加强管控'}状态。"
        )

    def _fallback_next(self, stats):
        inprog = [ts.task.name for ts in stats.task_stats if 0 < (ts.task.progress or 0) < 100]
        risks = [ts.task.name for ts in stats.task_stats if ts.risk_level != "正常"]
        parts = []
        if inprog:
            parts.append("持续推进 " + "、".join(inprog[:5]))
        if risks:
            parts.append("重点跟进风险项（" + "、".join(risks[:3]) + "）")
        if not parts:
            parts.append("维持项目正常推进")
        return "、".join(parts) + "。"

    # ---- 主入口 ----
    def render(self, stats: ProjectStats, use_ai: bool = True, today: Optional[date] = None,
               rules: Dict = None, prev_stats: Optional[Dict] = None,
               projects_data: Optional[List[Dict]] = None) -> Dict[str, str]:
        """生成周报。返回 {report, report_html, report_text, ai_used, error, numbers_corrected}。
        prev_stats：上一期的统计 dict，用于生成「与上周对比」章节。
        projects_data：分项目数据 [{name, stats}]，供精细模块（项目数据表/分项目区块）使用。"""
        today = today or date.today()
        rules = rules or {}

        # 模板若是 blocks（list），走模块化渲染
        if isinstance(self.template, list):
            return self.render_blocks(self.template, stats, use_ai=use_ai, today=today, rules=rules,
                                      prev_stats=prev_stats, projects_data=projects_data)

        overview = next_plan = ""
        ai_used = False
        error = None
        numbers_corrected = 0
        if use_ai:
            ai_res = self._ai_generate(stats)
            overview = ai_res.get("overview", "")
            next_plan = ai_res.get("next_plan", "")
            ai_used = "ai_error" not in ai_res
            error = ai_res.get("ai_error")
            numbers_corrected = ai_res.get("numbers_corrected", 0)
        else:
            overview = self._fallback_overview(stats)
            next_plan = self._fallback_next(stats)

        stats_html = self._stats_html(stats, rules)
        status_html = self._status_html(stats)
        risks_html = self._risks_html(stats, rules)
        stats_text = self._stats_text(stats)
        status_text = self._status_text(stats)
        risks_text = self._risks_text(stats)
        delta_html = self._delta_section(stats, prev_stats)
        delta_text = self._delta_section(stats, prev_stats, for_text=True)

        html_fields = {
            "project_name": stats.project.name or "未命名项目",
            "period": stats.project.period or "本期",
            "today": today.strftime("%Y-%m-%d"),
            "overview": overview,
            "stats_html": stats_html,
            "status_html": status_html,
            "risks_html": risks_html,
            "next_plan": next_plan,
            "delta_html": delta_html,
        }
        # 兼容旧占位符 stats_table/status_summary/risks
        html_fields["stats_table"] = stats_html
        html_fields["status_summary"] = status_html
        html_fields["risks"] = risks_html
        # 语义数据占位符：{完成率} {风险数} …（HTML 模板也可用）
        html_fields.update(_semantic_values(stats))

        report_html = self._render_template(self.template, html_fields)

        text_fields = {
            "project_name": stats.project.name or "未命名项目",
            "period": stats.project.period or "本期",
            "today": today.strftime("%Y-%m-%d"),
            "overview": overview,
            "stats_text": stats_text,
            "status_text": status_text,
            "risks_text": risks_text,
            "next_plan": next_plan,
            "delta_text": delta_text,
        }
        text_fields.update(_semantic_values(stats))
        report_text = self._render_template(DEFAULT_TEMPLATE_TEXT, text_fields)
        return {
            "report": report_html,   # 向前兼容
            "report_html": report_html,
            "report_text": report_text,
            "ai_used": ai_used,
            "error": error,
            "numbers_corrected": numbers_corrected,
        }

    @staticmethod
    def _render_template(template: str, fields: Dict[str, str]) -> str:
        result = template
        for k, v in fields.items():
            result = result.replace("{" + k + "}", str(v))
        result = re.sub(r"\{[a-z_]+\}", "", result)
        return result

    # ================= JSON Blocks 渲染 =================
    def render_blocks(self, blocks, stats: ProjectStats, use_ai: bool = True,
                      today: Optional[date] = None, rules: Dict = None,
                      prev_stats: Optional[Dict] = None,
                      projects_data: Optional[List[Dict]] = None) -> Dict[str, str]:
        """按 blocks 列表渲染周报。每个 block 由类型对应函数渲染。
        projects_data：分项目数据 [{name, stats}]，供「项目数据表/分项目区块」等精细模块使用。"""
        today = today or date.today()
        rules = rules or {}

        # 生成基础数据字段
        overview = next_plan = ""
        ai_used = False
        error = None
        numbers_corrected = 0
        if use_ai:
            ai_res = self._ai_generate(stats)
            overview = ai_res.get("overview", "")
            next_plan = ai_res.get("next_plan", "")
            ai_used = "ai_error" not in ai_res
            error = ai_res.get("ai_error")
            numbers_corrected = ai_res.get("numbers_corrected", 0)
        else:
            overview = self._fallback_overview(stats)
            next_plan = self._fallback_next(stats)

        ctx = {
            "stats": stats,
            "rules": rules,
            "overview": overview,
            "next_plan": next_plan,
            "project_name": stats.project.name or "未命名项目",
            "period": stats.project.period or "本期",
            "today": today.strftime("%Y-%m-%d"),
            "delta_html": self._delta_section(stats, prev_stats),
            "delta_text": self._delta_section(stats, prev_stats, for_text=True),
            "delta_rows": self._delta_parts(stats, prev_stats),
            "projects": projects_data or [],
        }

        html_parts, text_parts = [], []
        # 数据自动绑定型模块（人工可勾选关闭：binding=false → 输出占位提示）
        DATA_BOUND_TYPES = {"kpi", "stats_table", "status", "risk_list", "delta",
                            "transition", "project_table", "project_sections"}
        for block in blocks or []:
            typ = (block or {}).get("type", "")
            if typ in DATA_BOUND_TYPES and block.get("binding", True) is False:
                h_seg, t_seg = ("<p style='color:var(--muted)'>（该模块已关闭数据绑定，请在模板中开启）</p>",
                                "（该模块已关闭数据绑定）")
            else:
                renderer = BLOCK_RENDERERS.get(typ, BLOCK_RENDERERS["custom"])
                h_seg, t_seg = renderer(block, ctx, self)
            if h_seg:
                html_parts.append(h_seg)
            if t_seg:
                text_parts.append(t_seg)

        report_html = "\n".join(p for p in html_parts if p)
        report_text = "\n\n".join(p for p in text_parts if p)
        return {
            "report": report_html,
            "report_html": report_html,
            "report_text": report_text,
            "ai_used": ai_used,
            "numbers_corrected": numbers_corrected,
            "error": error,
        }


# ================= Block 渲染器 =================
def _block_title(block, ctx, _gen):
    t = block.get("title", "周报")
    t = _sub_vars(t, ctx)
    return f"<h1>{t}</h1>", t


def _block_meta(block, ctx, _gen):
    fmt = block.get("format", "周期：{period}")
    txt = _sub_vars(fmt, ctx)
    return f'<p class="meta">{txt}</p>', txt


def _block_heading(block, ctx, _gen):
    title = _sub_vars(block.get("title", "章节"), ctx)
    return f"<h2>{title}</h2>", title


def _block_overview(block, ctx, _gen):
    # placeholder 决定显示 overview 还是 next_plan
    ph = block.get("placeholder", "overview")
    content = ctx.get(ph, ctx.get("overview", ""))
    if ph == "next_plan":
        content = content or ctx["next_plan"]
    if not content:
        content = ctx["overview"] if ph == "overview" else ctx["next_plan"]
    return f"<p>{content}</p>", content


# stats 字段映射：给前端/MODULE 用的简短 key → stats 实际属性
STATS_FIELD_MAP = {
    "total": "total_tasks",
    "done": "done_count",
    "in_progress": "in_progress_count",
    "not_started": "not_started_count",
    "completion_rate": "completion_rate",
    "avg_progress": "avg_progress",
    "avg_target_progress": "avg_target_progress",
    "risk": "risk_count",
}
KPI_LABELS = {
    "total": "总任务数", "done": "已完成", "in_progress": "进行中",
    "not_started": "未开始", "completion_rate": "完成率",
    "avg_progress": "平均实际", "avg_target_progress": "平均目标", "risk": "风险",
}


def _block_kpi(block, ctx, _gen):
    stats = ctx["stats"]
    keys = block.get("keys") or ["total", "done", "completion_rate"]
    # 每个 KPI 卡片的绝对值副文案（绝对值 + 百分比 双呈现）
    abs_subs = {
        "total": f"{stats.total_tasks} 项",
        "done": f"{stats.done_count} 项",
        "in_progress": f"{stats.in_progress_count} 项",
        "not_started": f"{stats.not_started_count} 项",
        "completion_rate": f"{stats.done_count}/{stats.total_tasks} 已完成",
        "avg_progress": f"目标 {stats.avg_target_progress:.0f}%" if stats.avg_target_progress is not None else "",
        "avg_target_progress": f"实际 {stats.avg_progress:.0f}%",
        "risk": f"{stats.risk_count} 项",
    }
    items = []
    for k in keys:
        label = KPI_LABELS.get(k, k)
        attr = STATS_FIELD_MAP.get(k, k)
        val = getattr(stats, attr, None)
        if val is None:
            v = "-"
        elif isinstance(val, float):
            v = f"{val:.0f}%"
        else:
            v = str(val)
        items.append({"label": label, "value": v, "sub": abs_subs.get(k, "")})
    html = '<div style="display:flex;flex-wrap:wrap;gap:10px;margin:12px 0;">'
    for it in items:
        html += (f'<div style="flex:1;min-width:100px;border:1px solid #E4E7EC;'
                 f'border-radius:8px;padding:10px 14px;text-align:center;">'
                 f'<div style="font-size:20px;font-weight:700;color:#4A6CF7;">{it["value"]}</div>'
                 f'<div style="font-size:11px;color:#6B6B80;">{it["label"]}</div>'
                 + (f'<div style="font-size:11px;color:#9AA0B5;">{it["sub"]}</div>' if it["sub"] else '')
                 + '</div>')
    html += "</div>"
    text = "；".join(f"{it['label']}:{it['value']}" + (f"（{it['sub']}）" if it["sub"] else "") for it in items)
    return html, text


def _block_stats_table(block, ctx, _gen):
    stats = ctx["stats"]
    rows = [
        ("总任务数", str(stats.total_tasks)),
        ("已完成", str(stats.done_count)),
        ("进行中", str(stats.in_progress_count)),
        ("未开始", str(stats.not_started_count)),
        ("完成率", f"{stats.completion_rate}%"),
        ("平均目标进度", f"{stats.avg_target_progress:.0f}%" if stats.avg_target_progress is not None else "-"),
        ("平均实际进度", f"{stats.avg_progress:.0f}%"),
        ("风险项", str(stats.risk_count)),
    ]
    html = ['<table class="kpi" style="border-collapse:collapse;margin:10px 0;">']
    for k, v in rows:
        html.append(f'<tr><td style="border:1px solid #E4E7EC;padding:6px 16px;color:#6B6B80;">{k}</td>'
                    f'<td style="border:1px solid #E4E7EC;padding:6px 16px;font-weight:600;">{v}</td></tr>')
    html.append("</table>")
    text = "\n".join(f"{k}：{v}" for k, v in rows)
    return "\n".join(html), text


def _block_status(block, ctx, _gen):
    stats = ctx["stats"]
    done = [ts.task.name for ts in stats.task_stats if (ts.task.progress or 0) >= 100]
    doing = [ts.task.name for ts in stats.task_stats if 0 < (ts.task.progress or 0) < 100]
    pending = [ts.task.name for ts in stats.task_stats if (ts.task.progress or 0) <= 0]
    parts_h = []
    parts_t = []
    if done:
        parts_h.append("已完成：" + "、".join(done))
        parts_t.append("已完成：" + "、".join(done))
    if doing:
        parts_h.append("进行中：" + "、".join(doing))
        parts_t.append("进行中：" + "、".join(doing))
    if pending:
        parts_h.append("未开始：" + "、".join(pending))
        parts_t.append("未开始：" + "、".join(pending))
    if not parts_h:
        return "<p>本周无进行中任务。</p>", "本周无进行中任务。"
    return "<p>" + "<br>".join(parts_h) + "</p>", "\n".join(parts_t)


def _block_risk_list(block, ctx, _gen):
    stats = ctx["stats"]
    rules = ctx["rules"]
    risk_c = rules.get("color_risk", "#C0504D")
    warn_c = rules.get("color_warning", "#E65100")
    risks = [ts for ts in stats.task_stats if ts.risk_level != "正常"]
    if not risks:
        return "<p>本周无重大风险项，项目整体受控。</p>", "本周无重大风险项，项目整体受控。"
    html = ['<ul style="margin:10px 0 10px 18px;line-height:1.8;">']
    text = []
    for ts in risks:
        level = ts.risk_level
        color = risk_c if level == "风险" else warn_c
        reason = ts.risk_reason or "需关注"
        html.append(f'<li><span class="pill" style="background:{color};color:#fff;border-radius:10px;'
                    f'padding:2px 10px;">{level}</span> <b>{ts.task.name}</b>：{reason}</li>')
        text.append(f"{ts.task.name}（{level}）：{reason}")
    html.append("</ul>")
    return "\n".join(html), "\n".join(text)


def _block_plan(block, ctx, _gen):
    content = ctx["next_plan"]
    return f"<p>{content}</p>", content


def _block_delta(block, ctx, _gen):
    """环比上周：有上一期数据时输出对比段落，否则输出占位提示。"""
    html = ctx.get("delta_html", "")
    text = ctx.get("delta_text", "")
    if html:
        return html, text
    return "<p>（暂无上一期数据，环比章节将在保存历史后自动出现）</p>", "（暂无上一期数据）"


def _block_transition(block, ctx, _gen):
    """过渡叙述：基于环比生成「结构一致」的过渡段，保证各版本叙述风格统一。
    不依赖 AI，纯规则、确定性——每期读起来句式一致，便于对比。"""
    stats = ctx["stats"]
    rows = ctx.get("delta_rows") or []
    if not rows:
        return ("<p>本周为首期周报，已完成基线统计；下周起将自动与本期对比，形成连续的趋势叙述。</p>",
                "本周为首期周报，已完成基线统计；下周起将自动与本期对比，形成连续的趋势叙述。")
    # 提炼关键趋势
    cr = next((r for r in rows if r.startswith("完成率")), None)
    rk = next((r for r in rows if r.startswith("风险项")), None)
    np_ = next((r for r in rows if r.startswith("连续两周无进展")), None)
    trend = ""
    if cr:
        trend += cr + "；"
    if rk:
        trend += rk + "；"
    if np_:
        trend += np_ + "；"
    if not trend:
        trend = "；".join(rows) + "；"
    sentence = f"本周项目整体呈延续态势：{trend}请结合下方明细与风险清单跟进重点事项。"
    return f"<p>{sentence}</p>", sentence


def _block_project_table(block, ctx, _gen):
    """重点项目数据表格：按项目/分组对比 任务数/完成率/平均进度/风险/滞后（绝对值+百分比）。"""
    projects = ctx.get("projects") or []
    if not projects:
        return "<p>（无分项目数据：请在「数据」页先上传并选择项目）</p>", "（无分项目数据）"
    # 可配置只看部分项目（逗号分隔名称）
    only = [n.strip() for n in (block.get("projects") or "").split(",") if n.strip()]
    picked = [p for p in projects if (not only) or p.get("name") in only]
    if not picked:
        picked = projects
    def row(p):
        s = p.get("stats") or {}
        return (p.get("name") or "未命名", s.get("total", 0), s.get("completion_rate"),
                s.get("avg_progress"), s.get("risk", 0), s.get("delayed", 0))
    rows = [row(p) for p in picked]
    html = ['<table class="kpi" style="border-collapse:collapse;margin:10px 0;width:100%;">',
            '<tr><th style="border:1px solid #E4E7EC;padding:6px 12px;background:#F7F8FA;">项目</th>'
            '<th style="border:1px solid #E4E7EC;padding:6px 12px;background:#F7F8FA;">任务数</th>'
            '<th style="border:1px solid #E4E7EC;padding:6px 12px;background:#F7F8FA;">完成率</th>'
            '<th style="border:1px solid #E4E7EC;padding:6px 12px;background:#F7F8FA;">平均实际</th>'
            '<th style="border:1px solid #E4E7EC;padding:6px 12px;background:#F7F8FA;">风险</th>'
            '<th style="border:1px solid #E4E7EC;padding:6px 12px;background:#F7F8FA;">滞后</th></tr>']
    for r in rows:
        cr = f"{r[2]}%" if r[2] is not None else "-"
        ap = f"{r[3]}%" if r[3] is not None else "-"
        html.append(f"<tr><td style='border:1px solid #E4E7EC;padding:6px 12px;'><b>{r[0]}</b></td>"
                    f"<td style='border:1px solid #E4E7EC;padding:6px 12px;'>{r[1]}</td>"
                    f"<td style='border:1px solid #E4E7EC;padding:6px 12px;'>{cr}</td>"
                    f"<td style='border:1px solid #E4E7EC;padding:6px 12px;'>{ap}</td>"
                    f"<td style='border:1px solid #E4E7EC;padding:6px 12px;'>{r[4]}</td>"
                    f"<td style='border:1px solid #E4E7EC;padding:6px 12px;'>{r[5]}</td></tr>")
    html.append("</table>")
    text = "\n".join(" | ".join(str(x) for x in r) for r in rows)
    return "\n".join(html), text


def _block_project_sections(block, ctx, _gen):
    """分项目区块：每个项目渲染独立小节（项目名 + KPI + 风险清单），精细分项呈现。"""
    projects = ctx.get("projects") or []
    if not projects:
        return "<p>（无分项目数据）</p>", "（无分项目数据）"
    only = [n.strip() for n in (block.get("projects") or "").split(",") if n.strip()]
    picked = [p for p in projects if (not only) or p.get("name") in only] or projects
    html_parts, text_parts = [], []
    for p in picked:
        s = p.get("stats") or {}
        nm = p.get("name") or "未命名"
        cr = f"{s.get('completion_rate')}%" if s.get("completion_rate") is not None else "-"
        ap = f"{s.get('avg_progress')}%" if s.get("avg_progress") is not None else "-"
        risks = [t for t in (s.get("tasks") or []) if t.get("risk_level") not in (None, "", "正常")]
        html_parts.append(f"<h3 style='margin:14px 0 6px;color:#4A6CF7;'>📦 {nm}</h3>")
        html_parts.append(f"<p style='margin:4px 0;'>任务 {s.get('total',0)} 项 · 完成率 {cr} · 平均实际 {ap} · 风险 {s.get('risk',0)} · 滞后 {s.get('delayed',0)}</p>")
        text_parts.append(f"{nm}：任务 {s.get('total',0)} 项，完成率 {cr}，平均实际 {ap}，风险 {s.get('risk',0)}，滞后 {s.get('delayed',0)}")
        if risks:
            html_parts.append('<ul style="margin:6px 0 6px 18px;line-height:1.7;">' + "".join(
                f"<li><span class='pill' style='background:{'#C0504D' if t['risk_level']=='风险' else '#E65100'};color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;'>{t['risk_level']}</span> <b>{t['name']}</b>：{t.get('risk_reason') or '需关注'}</li>"
                for t in risks[:8]) + "</ul>")
            text_parts.append("风险：" + "；".join(f"{t['name']}（{t['risk_level']}）" for t in risks[:8]))
    return "\n".join(html_parts), "\n\n".join(text_parts)


def _block_custom(block, ctx, _gen):
    content = block.get("content", "")
    is_html = block.get("is_html", False)
    if is_html:
        rendered = _sub_vars(content, ctx)
        return rendered, _html_to_plain(rendered)
    txt = _sub_vars(content, ctx)
    esc = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<p>{esc}</p>", txt


BLOCK_RENDERERS = {
    "title": _block_title,
    "meta": _block_meta,
    "heading": _block_heading,
    "overview": _block_overview,
    "kpi": _block_kpi,
    "stats_table": _block_stats_table,
    "status": _block_status,
    "risk_list": _block_risk_list,
    "plan": _block_plan,
    "delta": _block_delta,
    "transition": _block_transition,
    "project_table": _block_project_table,
    "project_sections": _block_project_sections,
    "custom": _block_custom,
}


# ---------- 语义占位符（数据驱动，可在模板文本中任意使用） ----------
# 引用路径 = 数据在统计模型中的位置；计算方式 = 取数公式；供前端「占位符说明」展示与编辑
PLACEHOLDER_DOC = {
    "{完成率}":     {"path": "统计 → 完成率", "formula": "已完成任务数 / 总任务数 × 100%", "type": "百分比"},
    "{平均进度}":   {"path": "统计 → 平均实际进度", "formula": "Σ任务进度 / 任务数（有权重时按权重）", "type": "百分比"},
    "{平均目标}":   {"path": "统计 → 平均目标进度", "formula": "按目标进度曲线逐任务计算后取平均", "type": "百分比"},
    "{总任务}":     {"path": "统计 → 总任务数", "formula": "任务列表长度", "type": "整数"},
    "{已完成}":     {"path": "统计 → 已完成", "formula": "状态为「已完成」的任务数", "type": "整数"},
    "{进行中}":     {"path": "统计 → 进行中", "formula": "状态为「进行中」的任务数", "type": "整数"},
    "{未开始}":     {"path": "统计 → 未开始", "formula": "状态为「未开始」的任务数", "type": "整数"},
    "{滞后数}":     {"path": "统计 → 滞后", "formula": "状态为「已滞后」的任务数", "type": "整数"},
    "{风险数}":     {"path": "统计 → 风险", "formula": "风险等级 ≠ 正常的任务数", "type": "整数"},
    "{关键任务}":   {"path": "任务 → 人工标注「关键」", "formula": "勾选了「关键」的任务名（顿号分隔）", "type": "文本"},
    "{project_name}": {"path": "项目 → 名称", "formula": "生成时填写的项目名", "type": "文本"},
    "{period}":     {"path": "项目 → 周期", "formula": "生成时填写的统计周期", "type": "文本"},
    "{today}":      {"path": "系统 → 今天", "formula": "生成日期", "type": "文本"},
}


def _semantic_values(stats: ProjectStats) -> Dict[str, str]:
    """把统计对象转成语义占位符的值（键不含花括号）。"""
    return {
        "完成率": f"{stats.completion_rate}%",
        "平均进度": f"{stats.avg_progress}%",
        "平均目标": f"{stats.avg_target_progress:.0f}%" if stats.avg_target_progress is not None else "-",
        "总任务": str(stats.total_tasks),
        "已完成": str(stats.done_count),
        "进行中": str(stats.in_progress_count),
        "未开始": str(stats.not_started_count),
        "滞后数": str(stats.delayed_count),
        "风险数": str(stats.risk_count),
        "关键任务": "、".join(ts.task.name for ts in stats.task_stats if ts.is_critical) or "无",
    }


def _sub_vars(text: str, ctx: Dict) -> str:
    """替换占位符：基础（project_name/period/today）+ 语义数据占位符（{完成率} 等）。"""
    if not text:
        return ""
    result = text
    for k in ("project_name", "period", "today"):
        result = result.replace("{" + k + "}", str(ctx.get(k, "")))
    stats = ctx.get("stats")
    if stats is not None:
        for k, v in _semantic_values(stats).items():
            result = result.replace("{" + k + "}", v)
    return result


def _html_to_plain(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html or "").replace("\n", " ").strip()
