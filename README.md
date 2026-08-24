# PM Report Agent · AI 项目管理智能周报助手

把 Excel / CSV / Word / PDF 等**混合格式**的项目资料，自动提炼为结构化项目进度，用**规则引擎统计 + AI 生成专业周报**。

> 解决 PMO 项目管理的日常痛点：项目经理每周手工汇总多个单位/任务的进度、写周报，重复且易错。本工具让这件事"一键完成"。

## ✨ 新版：Web 操作面板

无需敲命令，打开浏览器即可使用 —— 上传数据、配置规则、定制模板、生成周报、管理历史，一站式完成。

```bash
# 启动面板
python -m uvicorn app:app --reload
# 浏览器打开 http://127.0.0.1:8000
```

**面板功能：**
- **数据**：拖拽上传 Excel/CSV/Word/PDF，自动解析并展示任务表格 + 统计看板（完成率/进度/风险）+ **数据体检**（缺失字段提示）；支持**✏ 编辑任务校对**、**导出明细 CSV**
- **规则配置**：可视化调节风险阈值（超期天数、临近预警、进度偏慢）+ **目标进度曲线**（线性/S 型），支持"恢复默认"清理旧规则、查看历史规则
- **模板**：可视化拖拽模块化模板编辑器（含 **环比上周** 模块），保存后自动延用
- **周报/历史**：一键生成周报（可开 AI 增强，**自动校正 AI 数字**、**与上周环比**），保存到历史、随时回溯查看
- **设置**：Web 面板直接配置 DeepSeek API Key（仅保存本地 config/keys.json，接口只返回脱敏信息）；**AI Token 用量统计**（输入/输出/缓存命中/估算费用）
- **省钱设计**：AI 结果本地缓存（相同数据不重复付费）、提炼只送规则预筛的候选行、生成前数据体检确认

## 核心能力

1. **多格式解析** —— 支持 Excel(.xlsx) / CSV / Word(.docx) / PDF(含扫描件 OCR) 四种输入
2. **统一进度模型** —— 无论来源如何，归一化为"任务、负责人、进度、状态、计划完成"的结构
3. **规则引擎统计** —— 完成率、总进度、滞后天数、风险分级（纯本地、确定性、不依赖网络）
4. **风险可解释** —— 超期/临近/偏慢三级分级 + **未按时启动预警** + **显式状态优先**（尊重文件里"已滞后/有风险"等人工标注）+ **任务依赖风险传递** + 单任务**偏慢豁免**
5. **AI 周报生成** —— 基于统计结果 + 你的模板，用大模型生成专业周报正文；**AI 数字自动校正**防编造
6. **周报环比** —— 自动与上一期对比（完成率/平均进度/风险/滞后），趋势一目了然
7. **模板可定制** —— 模块化 JSON blocks 可视化编辑，保存后**后续自动延用**

## 快速开始

```bash
# 1. 安装依赖（建议用 ARM 原生的 conda 环境，避免 Rosetta 编码问题）
conda activate base
pip install -r requirements.txt

# 2. 配置 DeepSeek API Key（三选一）
export DEEPSEEK_API_KEY="sk-xxx"          # 方式一：环境变量
# 编辑 config/keys.json                     # 方式二：配置文件
# 打开面板 →「设置」标签页填写               # 方式三：Web 面板（推荐，仅保存本地，接口只返回脱敏信息）

# 3. 一键生成周报
python run.py report 输入文件或目录 --period 2026-W33
```

## 示例

```bash
# 单个文件
python run.py report examples/input/进度表_示例.csv --period 2026-W33

# 整个目录（自动识别所有支持的格式）
python run.py report examples/input/ --period "8月第3周"

# 导出到文件 + 不带 AI（仅规则引擎）
python run.py report examples/input/ --no-ai -o 周报.md

# 使用自定义模板
python run.py report examples/input/ --template 我的模板.md
```

## 模板定制

默认模板包含以下占位符，会在生成时替换：

| 占位符 | 说明 |
|--------|------|
| `{project_name}` | 项目名称 |
| `{period}` | 统计周期 |
| `{today}` | 生成日期 |
| `{overview}` | 进展综述（AI 或规则生成） |
| `{stats_table}` | 核心数据表格 |
| `{status_summary}` | 已完成/进行中摘要 |
| `{risks}` | 风险与关注事项 |
| `{next_plan}` | 下周计划 |

**保存自定义模板**（后续生成自动延用）：
```bash
python run.py template --file 我的模板.md    # 导入并保存
python run.py template --show                 # 查看当前模板
python run.py template --reset                # 恢复默认
```

## 系统架构

```
       Web 操作面板（FastAPI + 浏览器）
        │  上传数据 / 配规则 / 编辑模板 / 生成 / 历史
        ▼
输入（Excel/CSV/Word/PDF）         config/rules.json
        │                                ▲
        ▼                                │ 规则可配置
┌──────────────┐     ┌─────────────────┐────┐
│  解析器层      │ ──► │   统一进度模型     │  规则引擎
│  tabular/text │     │  Project + Task   │  (阈值可调)
└──────────────┘     └─────────────────┘────┘
                              │
                              ▼
                     ┌──────────────┐
                     │  AI 周报生成   │  DeepSeek + 模板(可定制)
                     └──────────────┘
                              │
                              ▼
                        history/（历史周报）
```

## 目录结构

```
pm-report-agent/
├── run.py                 # CLI 入口
├── app.py                 # FastAPI Web 面板后端
├── web/
│   └── index.html         # 操作面板前端
├── requirements.txt       # 依赖
├── config/
│   ├── keys.json          # DeepSeek API Key 配置
│   └── rules.json         # 规则配置（默认值，可被面板覆盖）
├── pmo_report/
│   ├── models.py          # 数据模型
│   ├── engine.py          # 规则引擎（阈值可配置）
│   ├── rules.py           # 规则配置管理 + 历史/清理
│   ├── report.py          # 周报生成器 + 模板系统
│   ├── ai.py              # DeepSeek 客户端/AI增强
│   └── parsers/           # 解析器（Excel/CSV/Word/PDF）
├── templates/             # 周报模板
├── history/               # 历史周报（运行期生成）
└── examples/              # 示例数据
```

## 环境要求

- Python 3.10+（**ARM Mac 建议用 miniforge/conda 的 ARM Python，避免 Rosetta 下 urllib3 编码问题**）
- DeepSeek API Key（可选，不配则用规则引擎回退文案）
- PDF OCR 需 tesseract（`brew install tesseract tesseract-lang`）——可选，纯文本 PDF 不需要

## 许可证
MIT
