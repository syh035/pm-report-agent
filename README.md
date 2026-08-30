# PM Report Agent · AI 项目管理智能周报助手

把 Excel / CSV / Word / PDF 等**混合格式**的项目资料，通过 **AI 分析**自动提炼为结构化项目进度（任务/指标/里程碑/风险/决策），并**按你的模板原地更新**生成专业周报——论述风格、用词、格式与模板保持一致。

> 解决 PMO 项目管理的日常痛点：项目经理每周手工汇总多个单位/任务的进度、写周报，重复且易错。本工具让这件事"上传 → 分析 → 按模板生成"一条线完成。

## 核心思路：主体是 AI

- **上传即分析**：文件上传后由分析 AI 提取结构化条目（任务/指标/里程碑/风险/决策）存入分类仓；
- **看板只展示 AI 提取的数据**：无 AI 数据时不显示任何规则推算的假 KPI；
- **规则参数 = AI 的处理约定**：超期天数/偏慢阈值/忽略词/状态词等作为指令注入分析 AI，由 AI 按约定判断（风险等级、重点关注标记），不是规则引擎代算；
- **模板原地更新**：生成时把模板原文交给生成 AI，只替换与数据对应的位置（数字/日期/状态/任务名），其余文字、风格、格式原样保留。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
# 注意：PDF/Word 解析用到 markitdown，需 Python 3.10+；PDF OCR 需 tesseract（可选）

# 2. 配置 DeepSeek API Key（三选一）
export DEEPSEEK_API_KEY="sk-xxx"          # 方式一：环境变量
# 编辑 config/keys.json                     # 方式二：配置文件（gitignored）
# 打开面板 →「AI 配置库」填写              # 方式三：Web 面板（仅保存本地，接口只返回脱敏信息）

# 3. 启动 Web 面板
python -m uvicorn app:app --reload
# 浏览器打开 http://127.0.0.1:8000
```

## 面板功能

- **数据**：拖拽上传 Excel/CSV/Word/PDF（上传即 AI 分析）；左侧源文件列表（可分组，一个周报引用整组多数据集）；点击源文件 → Excel 式表格展示 AI 提取条目，字段自适应（指标→数值、任务→进度/状态、说明类→整段文本），并可自定义显示哪些「性质列」
- **看板**：KPI 卡 + 分类区块 + ★重点关注专区（性质来自分析要求，命中即统计）；「⚙ 定制看板」对话式调整布局（顺序/显隐/颜色/文案，配置存本地，只读访问配置库）
- **模板 & 生成**：上传模板（Word/PDF/HTML）→ AI 解析 → 确认入库（自动保留模板原文）；生成 = 按模板原地更新，支持整组数据源引用
- **设置**：Web 面板直接配置 DeepSeek API Key（仅保存本地 config/keys.json，接口只返回脱敏信息）

### 数据库中心（📚 按钮）

- **原始数据库**：上传的原始文件按日期归档，删除留痕可撤销
- **文稿库**：生成的日/周报归档、查看、重命名、删除
- **模板库**：命名入库的模板，区分周报/日报，按日期/分类选取
- **规则库**：分析要求 + 生成要求（对话式添加/文档导入/可改可删）；参数约定（超期/偏慢/忽略词/状态词/列名映射）作为分析 AI 的处理指令
- **AI 配置库**：API Key / 模型 / 接口地址
- **提示词库**：全部 AI 提示词（system/user/few-shot），可编辑保存、版本历史回退

## 系统架构

```
       Web 面板（FastAPI + 浏览器）
         │  上传数据 / 看板 / 模板 / 生成
         ▼
 输入（Excel/CSV/Word/PDF）
         │
         ▼  markitdown 转文本 + 处理约定注入
  分析 AI（提取结构化条目） ──► 分类仓（SQLite）
         │                              │
         │                    看板（只展示 AI 提取条目）
         │                              │
         ▼                              ▼
  生成 AI ◄── 模板原文（原地更新）   ⚙ 定制看板（配置存本地）
         │
         ▼
    周报 HTML（可导出 Word / 存入文稿库）
```

## 目录结构

```
pm-report-agent/
├── app.py                 # FastAPI Web 后端
├── run.py                 # CLI 入口（旧版，仍可用）
├── requirements.txt       # 依赖
├── web/
│   ├── index.html         # 主面板（数据/看板/模板&生成/设置）
│   ├── libraries.html     # 数据库中心入口
│   └── lib/               # 各独立库页面 + chatbox/formkit/cal 组件
├── pmo_report/
│   ├── ai.py              # DeepSeek 客户端（缓存/Key 管理）
│   ├── ai_analysis.py     # 分析 AI：提取结构化条目（处理约定注入）
│   ├── ai_dialogue.py     # 通用对话弹窗后端（规则/模板/看板定制）
│   ├── ai_pipeline.py     # 文档转文本 / AI 分析辅助
│   ├── prompts.py         # 3 类提示词（分析/生成/规则配置）+ 版本历史
│   ├── datastore.py       # SQLite：源文件/条目/模板/分组/留痕
│   ├── rules.py           # 规则配置（参数→分析 AI 指令）
│   ├── dataset.py         # 数据集板块解析（xlsx）
│   └── parsers/           # 文本/表格解析器
├── config/                # 运行期配置（全部 gitignored）
├── data_sources/          # 上传文件归档（运行期生成，gitignored）
└── examples/              # 示例数据
```

## 环境要求

- Python 3.10+（ARM Mac 建议用 miniforge/conda 的 ARM Python）
- DeepSeek API Key（不配置则无法分析/生成——本工具主体是 AI，不配 Key 时仅保留原始文件，不产生假数据）
- PDF OCR 需 tesseract（`brew install tesseract tesseract-lang`）——可选，纯文本 PDF 不需要

## 许可证

MIT
