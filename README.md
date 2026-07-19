# boss-auto-apply — Boss直聘智能投递助手

为求职者自动完成「理解简历 → 搜索岗位 → 针对性定制 → 生成 PDF/PNG → 自动投递」全闭环的工具。
**最终目标是找到工作，不是「投出去」。**

## ✨ 核心特性

- **多格式简历解析**：支持 PDF / DOCX / Markdown / TXT 四种格式，docling 主 + PyMuPDF/python-docx 兜底双引擎。
- **交互式简历审查**：简历解析后自动识别存疑点（经历断层、数据缺失、技能模糊），CLI 逐条提问并附优化建议。
- **HRBP 体检闸门**（ADR-0001）：简历投出前由 HRBP agent 体检（工作年限 / 禁忌词 / 数据可答性），用户确认后才解锁投递。
- **数据诚信优先**（ADR-0002）：所有量化数据「可信 + 可验证 + 有基数参照」三者同时满足，禁止凭空编造。
- **候选人画像 candidate_profile**（ADR-0005）：从简历一次推导（目标岗位/资历级别/核心行业），下游 matcher/pipeline 通用复用。**不硬编码任何角色**——产品经理/Java/销售/数据分析师都能用同一套代码。
- **履历母库 master.json**：定制只选材不生成，杜绝幻觉。
- **三维 matcher 评分**：硬技能（0.75）+ 业务领域（0.25）+ 资历级别 + 岗位类别一票否决，防 overqualified（如资深 PM 投助理岗）。
- **双引擎浏览器自动化**（ADR-0003）：DrissionPage 负责登录+搜索+详情页，nodriver 负责聊天页发送（绕过 zpAegis 反爬）。
- **断点续传**：任何步骤失败都留在原状态，`resume` 自动重试该步。

## 🚀 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone <repo-url> boss-auto-apply
cd boss-auto-apply

# 一键安装（建 conda 3.12 venv + Python 依赖 + typst + 思源黑体 + brilliant-CV 模板）
bash setup.sh
```

或手动安装：

```bash
conda create -n boss-auto python=3.12 -y
conda activate boss-auto
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config/config.example.yaml config/config.yaml
```

编辑 `config/config.yaml`，**必须修改**以下内容为你自己的信息：

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `cities` | 求职城市 | `["上海"]` |
| `target_constraints.primary_direction` | 你的求职主方向（**提示**，v2 后 LLM 可推荐池外方向） | `"后端开发"` |
| `target_constraints.sub_directions` | 子方向+关键词（v2 后降级为 keywords_hint，LLM 可自主推荐 master 中体现的其他方向） | 见示例文件 |
| `target_constraints.salary` | 城市→薪资带 | `{"上海": "15-25K"}` |
| `target_constraints.degree` | 你的学历 | `"本科"` |
| `target_constraints.experience` | 工作年限 | `"3-5年"` |

### 3. 登录 Claude CLI（LLM 引擎）

```bash
claude auth login
```

> 项目通过 `claude` CLI 调用 LLM（subprocess），不直连 API。需先安装 Claude CLI 并保证在 PATH 中。

### 4. 放简历

把简历放到 `input/` 目录，支持以下格式（在 `config.yaml` 的 `paths.resume_input` 指定文件名）：

| 格式 | 扩展名 | 解析器 | 说明 |
|------|--------|--------|------|
| PDF | `.pdf` | docling → PyMuPDF | 推荐，格式还原最好 |
| Word | `.docx` | docling → python-docx | 推荐格式 |
| Markdown | `.md` | TextRuleParser | 直接读取，保留 `#` 结构 |
| 纯文本 | `.txt` | TextRuleParser | 按锚点识别标题 |

```bash
# 示例：放一份 PDF 简历
cp ~/Desktop/my_resume.pdf input/resume.pdf

# 或放一份 DOCX
cp ~/Desktop/my_resume.docx input/resume.docx
# 并在 config.yaml 改 paths.resume_input: "input/resume.docx"
```

### 5. 跑预检

```bash
conda run -n boss-auto python -m boss_auto_apply preflight
```

检查环境（Python/typst/字体/简历文件/配置）是否就绪。

### 6. 跑 dry-run（全链路模拟）

```bash
conda run -n boss-auto python -m boss_auto_apply dry-run --non-interactive
```

全 mock 模式跑完：**解析 → 交互审查 → 体检 → 画像 → 搜索 → 匹配 → 定制 → PDF → PNG**。

## 📋 CLI 命令

| 命令 | 说明 |
|------|------|
| `preflight` | P1–P9 前置检查（M3 加跑 P10–P14） |
| `parse` | 只跑 F1 → master.json（含交互审查） |
| `hrbp-check` | 只跑 F1.6 体检（暂停等确认） |
| `profile` | 只跑 F1.5 方向推荐（暂停等确认） |
| `dry-run` | F1→F6 全链路，全 mock，不触网（止于 image_ready） |
| `run` | **F1→F7 全链路真发送**（M3：点击沟通→发话术→发简历图片） |
| `resume` | 断点续传（含发送中途失败的 greeted/text_sent 续传） |
| `status` | 打印各状态 job 计数 + 最近 run_log |
| `login` | 首次扫码登录 Boss直聘（保存专用 Chrome profile） |

通用 flag：`--config <path>`（全局）。
子命令 flag（`cmd --flag` 顺序）：`--non-interactive`、`--target <name>`。
`run` 专属：`--yes` / `-y`（跳过真发送风险确认，如 `python -m boss_auto_apply run --yes`）。

### 真发送工作流（M3）

```bash
# 1. 首次登录（保存 cookie 到专用 profile，后续复用）
python -m boss_auto_apply login

# 2. dry-run 生成 image_ready jobs（不触网）
python -m boss_auto_apply dry-run --non-interactive

# 3. 真发送（复用 dry-run 阶段的 jobs）
python -m boss_auto_apply run            # 会弹风险确认
python -m boss_auto_apply run --yes      # 跳过确认（已知晓风险）

# 4. 中途失败断点续传
python -m boss_auto_apply resume
```

`run` 会依次执行：① M3 预检（DrissionPage/Chrome/profile/Pillow）→ ② 登录态验证 → ③ 风险确认 → ④ **流式真发送**。采用「采一个投一个」流式架构：搜到一个岗位 → 立即抓JD→匹配→定制→PDF→PNG→发送 → 再搜下一个，避免长时间等待。

**v2 限流策略**（2026-07-19 重构）：处理阶段（matcher/tailor/PDF/PNG）不预扣限流名额，每个岗位 5 分钟的 LLM 耗时天然满足 interval 间隔要求；只在真发送时（`Sender.send_application`）才 acquire 限流名额。频次类拒绝（wait ≤ 180s）会 sleep 等够重试一次，不再"沉默丢岗"；熔断/日上限类拒绝（wait > 180s）整批停止。内置六道闸门（熔断/工作日白天/预热/日上限/连投/间隔）和三态熔断器降低封号风险，但**不能完全消除**。

**v2 双引擎 profile 互斥自动处理**：`sender.send_application` 在 DrissionPage 完成 greet 后会主动 `quit` Chrome，再交给 nodriver 启动发送，**用户不需要在 run 中途手动关 Chrome**（仍需在 run 启动前关闭日常 Chrome 避免冲突）。

> ⚠️ **`run` 前必须先关闭所有 Chrome 窗口**：
> ```bash
> pkill -9 -f "Google Chrome"
> ```
> 原因：nodriver 与 DrissionPage 不能共用同一 `user_data_dir`（会互相覆写 cookies，ADR-0003）。
>
> 真发送用 nodriver 引擎（`config.sender.driver: "nodriver"`），它绕过 zpAegis 反爬。项目对 nodriver 做了三项工程适配（见 `nodriver_sender.py`）：预启动 Chrome 并轮询端口就绪（解决 nodriver 自带 2.75s 超时太短的问题）、contenteditable 输入用 `execCommand('insertText')`（`send_keys` 对 Boss 输入框无效）、`evaluate` 返回值用 `JSON.stringify` 包装（规避 nodriver 0.50.3 的 RemoteObject 序列化问题）。

## ⚙️ 配置详解

`config/config.yaml` 分为以下段落：

| 段落 | 说明 |
|------|------|
| `pipeline` | 流程开关（dry-run/体检/审查/确认） |
| `cities` | 求职城市列表 |
| `target_constraints` | 方向/薪资/学历/禁忌词约束 |
| `search` | 搜索引擎（mock/drissionpage） |
| `sender` | M3 发送配置（driver/user_data_dir/stealth） |
| `llm` | LLM 配置（claude_bin/model_*/timeout） |
| `limits` | 限流（daily_total/interval/burst/warmup） |
| `circuit_breaker` | 熔断器（连续失败/失败率阈值） |
| `pdf` | PDF 生成（template/font/typst_bin） |
| `paths` | 文件路径（resume_input/master_json/db） |

校验规则见 `config/config.schema.json`（JSON Schema + Pydantic 双校验）。

## 📁 目录结构

```
boss-auto-apply/
├── boss_auto_apply/           # Python 包
│   ├── core/                  # 核心模块
│   │   ├── parser.py          # F1 多格式简历解析 + F1.55 候选人画像补全（ADR-0005）
│   │   ├── qa.py              # F1.7 简历存疑点审查 + 交互问答
│   │   ├── hrbp_check.py      # F1.6 HRBP 体检闸门（含 candidate_profile 校验）
│   │   ├── profiler.py        # F1.5 方向推荐
│   │   ├── matcher.py         # F3 JD 匹配（三维：硬技能+业务领域+资历级别）
│   │   ├── tailor.py          # F4 简历定制 + F7 个性化招呼生成
│   │   ├── generator.py       # F5 PDF 生成（含 typst 大小写归一化）
│   │   ├── imager.py          # F6 PNG 生成
│   │   └── sender.py          # F7 话术+发送编排
│   ├── browser/               # 浏览器自动化
│   │   ├── manager.py         # DrissionPage 浏览器管理
│   │   ├── web_searcher.py    # 网页搜索
│   │   ├── web_greeter.py     # 网页沟通点击（v3.1：点头次等 8s 让按钮变「继续沟通」）
│   │   ├── web_chat_sender.py # DrissionPage 聊天发送
│   │   ├── nodriver_sender.py # nodriver 聊天发送（v3.1：点继续沟通让 Boss 自动跳转带参数 URL）
│   │   └── image_util.py      # 图片预处理共享函数
│   ├── prompts/               # LLM prompt 模板（核心资产）
│   │   ├── parser.md          # F1 简历结构化 + 候选人画像推导方法论
│   │   ├── hrbp_check.md      # F1.6 体检
│   │   ├── profiler.md        # F1.5 方向推荐
│   │   ├── matcher.md         # F3 三维匹配（含灰区场景判断）
│   │   ├── tailor.md          # F4 简历定制
│   │   └── greet.md           # F7 个性化招呼
│   ├── pipeline.py            # 状态机主流程
│   ├── config.py              # 配置加载 + 校验
│   └── ...
├── config/                    # 配置 + schema
├── tests/                     # 单元 + 集成 + fixtures
├── data/                      # 运行时产物（gitignore）
├── input/                     # 简历输入（gitignore）
└── templates/brilliant-cv/    # Typst 简历模板（git clone）
```

## 🧪 测试

```bash
conda run -n boss-auto python -m pytest tests/ -v
```

测试覆盖：407+ test case（单元 + 集成 + prompt 回归 + DB 迁移 + 候选人画像 + typst 大小写归一化）。

## 📐 架构决策

- [ADR-0001](docs/adr/0001-hrbp-health-check-mandatory.md) — HRBP 体检闸门（投递前强制体检）
- [ADR-0002](docs/adr/0002-data-integrity-over-numbers.md) — 数据诚信三规则（禁止凭空编造）
- [ADR-0003](docs/adr/0003-nodriver-for-chat-page.md) — 双引擎浏览器架构（nodriver 绕过 zpAegis）
- [ADR-0004](docs/adr/0004-multi-format-resume.md) — 多格式简历解析（pdf/docx/md/txt）
- [ADR-0005](docs/adr/0005-candidate-profile-generalization.md) — 候选人画像通用化基座（取代产品经理硬编码）

## ⚠️ 合规与风险

本项目仅供**学习研究**。Boss直聘用户协议明确禁止自动化操作，实际使用存在**账号风控/封号风险**，风险由用户自担。

- `dry-run` 模式不触网、不发起任何对 zhipin.com 的写请求。
- `run` 模式（M3+）会真实操作 Boss直聘网页，**请充分理解风险后再使用**。
- 内置限流器（daily_total / min_interval / burst_size）和熔断器（连续失败/失败率阈值）降低风控触发概率，但**不能完全消除风险**。

## ❓ FAQ

**Q: docling 装不上怎么办？**
A: docling 需 Python 3.12。装不上时代码会自动降级到 PyMuPDF（PDF）或 python-docx（DOCX），闭环不破。

**Q: nodriver 和 DrissionPage 有什么区别？**
A: DrissionPage 用于登录+搜索+详情页（zpAegis 不拦这些）；nodriver 用于聊天页发送（zpAegis 检测 CDP Runtime.enable 阻断聊天页 SPA，nodriver 刻意不调此命令绕过检测）。详见 ADR-0003。

**Q: 简历用哪种格式最好？**
A: PDF 还原度最高（推荐）。DOCX 次之。Markdown/TXT 适合纯文本简历（如开发者）。

**Q: 候选人专属禁忌词怎么配置？**
A: 在 `config.yaml` 的 `target_constraints.forbidden_words_extra` 添加数组，如 `["300%", "7年"]`。这些词会与通用禁忌词（精通/100%/完美/极致）一起扫描。

> 注：禁忌词政策为**只告警不阻断**（2026-07-16 调整）。`validate_tailored` 发现禁忌词时只记 warning，不会触发 LLM 重试或中断流程。仅「幻觉项目」和「工作年限超限」会阻断。

## 🤝 Contributing

欢迎提交 Issue 和 PR。开发前请阅读：

- `AGENTS.md` — AI agent 开发指南
- `CONTEXT.md` — 领域语言和数据表述标准
- `docs/DESIGN.md` — 14 章设计文档（唯一蓝图）

## 📄 License

MIT（仅供学习研究，使用者自行承担合规风险）。
