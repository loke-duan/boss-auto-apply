# BOSS 直聘智能投递助手 — 产品需求文档（PRD）

| 项目名称 | BOSS 直聘智能投递助手（代号 `boss-auto-apply`） |
| --- | --- |
| 文档版本 | v1.2（经资深 PM + HRBP 审阅，纳入 9 轮 grill 拷问结论） |
| 创建日期 | 2026-07-07 |
| 最近更新 | 2026-07-07（v1.2：HRBP 视角发现并修正 5 个致命问题；新增 F1.6 体检、数据诚信 ADR、岗位池验证、3 子方向） |
| 文档性质 | 产品需求文档，用于与用户对齐需求理解、指导后续开发 |
| 撰写角色 | 产品经理 + HRBP（基于三轮 subagent 脑暴 + 18 次求证 + 9 轮 grill） |
| 配套文档 | [CONTEXT.md](./CONTEXT.md)（领域语言）、[docs/adr/](./docs/adr/)（架构决策记录） |

---

## 0. 阅读指引

本 PRD 的每一项关键决策都标注了**来源**：
- `[决策 2026-07-06]`：用户在求证环节明确选择
- `[决策 2026-07-07]`：用户对 10 项待确认清单的最终决策（v1.1 已全部固化，见附录 D）
- `[调研结论]`：subagent 实地查证 GitHub 项目/README/技术文档后得出
- `[默认假设]`：v1.0 中未直接求证的合理默认值，**v1.1 已全部转为用户决策**

> ✅ v1.1 起，文档末尾「待确认清单」已清空（10 项全部确认完毕，详见附录 D）。

---

## 1. 项目背景

### 1.1 用户痛点
求职者在 BOSS 直聘上手动投递简历存在三大痛点：
1. **岗位筛选累**：需要在海量岗位中逐一浏览、判断匹配度
2. **简历针对性差**：一份通用简历投所有岗位，命中率低；为每个岗位手工调整又太耗时
3. **投递动作重复**：打招呼、发简历、记录进度，全是机械重复劳动

### 1.2 产品愿景
做一个**全自动、可配置、安全的 BOSS 直聘智能投递助手**：
> 用户放入一份简历 PDF + 一份配置文件 → 系统自动完成「理解求职者 → 搜索匹配岗位 → 每岗定制简历 → 生成 PDF → 自动发送（打招呼 + 图片简历）」全闭环，全程严格限流、可断点续传、可人工介入。

### 1.3 使用场景与范围边界 `[决策 2026-07-06]`
- **使用场景**：纯个人自用（不分享、不部署给他人、不收费、不开源、不商业化）
- **合规说明**：本项目仅供学习研究。BOSS 直聘用户协议明确禁止自动化操作，实际使用存在账号风控/封号风险，**风险由用户自担**。系统通过严格限流 + 风控熔断 + 半自动降级来**降低**而非消除风险。

---

## 2. 求职者真实画像与核心诉求（v1.2 重写，基于真实简历）

### 2.1 求职者画像（基于简历实测 + HRBP 体检）`[v1.2 修正]`
- **身份**：张三（虚构样例候选人），SEO 增长 / SaaS 内容运营方向
- **学历**：本科（示例理工大学 · 市场营销）`[HRBP 决策：config degree=本科]`
- **真实工作年限**：5.5 年（2019.07–2025.03，三段可验证经历）`[HRBP 决策：修正简历「7年」表述]`
- **当前状态**：2025.03 起自由职业，为 3+ SaaS 客户做 SEO 咨询 `[HRBP 决策：填充空窗期，B+C 策略]`
- **最强护城河**：SaaS 产品官网 SEO 体系 0→1 搭建、技术 SEO + 内容 SEO 双轮驱动
- **次要能力**：开发者社区内容矩阵、长尾词布局、跨部门线索流转闭环
- **意向城市**：上海（15-22K）/ 武汉（10-15K）`[HRBP 决策：务实薪资带]`
- **核心求职方向**：SEO 增长为主 `[HRBP 决策：聚焦 SaaS/工具类赛道]`

### 2.2 核心诉求（v1.2 修正优先级）
1. 🔴🔴🔴 **找到工作**（终极目的，不是「投出去」）`[v1.2 新增，HRBP 视角]`
2. 🔴🔴 **简历诚信**（背调/面试可验证，禁伪造）`[v1.2 新增，HRBP 视角]`
3. 🔴 **安全第一**：主账号不能被封
4. 🟠 **省心**：全自动，配置好就能跑
5. 🟡 **可控**：投什么岗位、什么地区、什么薪资我说了算
6. 🟡 **质量**：每岗一份定制简历，不是群发垃圾

### 2.3 非目标用户（明确排除）
- ❌ 求职中介、批量代投服务商（违反 get_jobs 许可证 + 违反 BOSS 协议）
- ❌ 完全不懂技术的小白用户（本项目需要配置和命令行操作）

---

## 3. 核心流程（端到端）

```
┌─────────────────────────────────────────────────────────────────────┐
│                         用户一次性准备                                  │
│  ① 把简历 PDF 放到固定位置（input/resume.pdf）                         │
│  ② 填写 config.yaml（限流参数/城市/薪资框架/开关，不填具体岗位方向）     │
│  ③ 首次启动：手动扫码登录 Boss，登录态持久化到日常 Chrome profile       │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           自动化主流程                                  │
│                                                                       │
│  [1]  解析简历      用 Docling 把 input/resume.pdf → 结构化 master.json │
│        ↓                                                              │
│  [1.6] HRBP 体检★新  HRBP agent 扫禁忌词/数据风险/面试可答性 → 体检报告  │
│        ↓             ⏸️ 用户确认体检建议（改年限/改表述/填真实数据）      │
│  [1.5] 简历画像      LLM 分析 master.json，推断适合的行业/方向/关键词    │
│        ↓             生成推荐 target 列表 → ⏸️ 人工确认后继续            │
│  [2]  搜索岗位      用 boss-cli 按确认的 target 搜索，拉取 JD，去重入库  │
│        ↓                                                              │
│  [3]  分析匹配      LLM 提取 JD 技能、计算匹配度，过滤不匹配岗位          │
│        ↓                                                              │
│  [4]  定制简历      LLM 从 master.json 选材+重排+润色，生成 .typ 源文件  │
│        ↓                                                              │
│  [5]  生成 PDF      brilliant-CV（Typst 原生）编译 .typ → PDF + PNG     │
│        ↓                                                              │
│  [6]  转图片        Typst 原生 PNG 输出（或 PyMuPDF 兜底转 JPG）         │
│        ↓                                                              │
│  [7]  限流等待      RateLimiter：日上限15、随机间隔、工作日白天、熔断    │
│        ↓                                                              │
│  [8]  发送打招呼    boss-cli greet（文字）+ DrissionPage 发图片简历      │
│        ↓                                                              │
│  [9]  记录状态      SQLite 更新 job 状态，支持断点续传                    │
│                                                                       │
│  循环 [2]-[9] 直到：达到日上限 / 无新岗位 / 触发风控熔断                  │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.1 流程关键约束（重要）
| 约束 | 说明 | 来源 |
| --- | --- | --- |
| **每岗一份定制简历** | 不是一份通用简历群发，而是为每个目标岗位单独定制 | `[决策 2026-07-06]` |
| **打招呼 + 发图片简历** | 每次投递包含两步：① 文字打招呼（boss-cli greet）② 图片简历附件（DrissionPage 发送） | `[决策 2026-07-06]` |
| **发图需等待沟通关系** | Boss 要求双方建立「沟通关系」后才能发图，打招呼后需延迟再发图（参考 get_jobs 逻辑） | `[调研结论]` |
| **求职方向动态生成** | target 不是 config 手填，而是 LLM 分析简历后推荐、用户确认 | `[决策 2026-07-07]` |
| **双城市同方向** | 武汉 + 上海，两个城市投相同方向（由简历分析得出），生成 2×N 套 target | `[决策 2026-07-07]` |

---

## 4. 功能需求详述

### 4.1 简历解析模块（F1）

**功能描述**：把用户放入的 PDF 简历解析成结构化的「履历母库」master.json。

| 项 | 说明 |
| --- | --- |
| 输入 | `input/resume.pdf`（固定位置 + 固定文件名）`[决策 2026-07-06]` |
| 输出 | `data/master.json`，结构化履历库（基本信息/技能模块/项目池/经历池） |
| 技术选型 | **Docling**（IBM Research，25K★，PDF 基准测试第一）`[调研结论]` |
| 兜底 | markitdown（用户提供）作为 Office 格式简历的备用解析器 |
| LLM 增强 | Docling 出 Markdown/JSON 后，由 zcode agent 整理成结构化履历库 |

**为何不用用户提供的 markitdown？** `[调研结论]`
- markitdown（微软，80K★）强项是 Office 格式，**PDF 是它短板**：在 12 款工具基准测试中排名倒数第二，无法识别标题/页脚/表格（GitHub Issue #296）
- 简历普遍含多栏布局 + 表格，正是 markitdown 弱项
- 中文 PDF 还有乱码风险（取决于字体嵌入）
- → **markitdown 降级为兜底，主解析器换 Docling**

**master.json 结构（履历母库，降本核心）**：
```json
{
  "basics": { "name": "...", "phone": "...", "email": "...", "city": "..." },
  "skill_modules": {
    "fastapi": { "summary": "...", "bullet_points": [...], "projects": [...] },
    "postgres": { ... }
  },
  "projects": [ { "name": "...", "stack": [...], "highlights": [...] } ],
  "experiences": [ { "company": "...", "position": "...", "highlights": [...] } ]
}
```

### 4.1.6 HRBP 简历体检模块（F1.6）★v1.2 新增 `[HRBP 决策 2026-07-07]`

**功能描述**：在简历解析后、方向推荐前，由 HRBP agent 对 master.json 做「简历体检」，输出体检报告供用户确认。**这是 v1.2 因用户反馈「禁忌词不能一刀切，每份简历要 HRBP 深度挖掘」而新增的强制环节**（详见 [ADR-0001](./docs/adr/0001-hrbp-health-check-mandatory.md)）。

| 项 | 说明 |
| --- | --- |
| 触发时机 | 简历解析（F1 出 master.json）后、简历画像分析（F1.5）之前 |
| 执行者 | zcode agent + LLM，扮演「资深 HRBP」角色 |
| 暂停点 | ⏸️ 输出体检报告后暂停，等用户确认/修改/补充 |

**HRBP 体检清单（针对虚构样例简历 resume.sample.pdf 已检出）**：
1. **工作年限诚信**：声明 7 年 vs 可验证 5.5 年 → 修正为 5.5 年
2. **空窗期**：2025.03 后约 15 个月 → 填充自由职业期（B+C 策略，3+ 客户）
3. **禁忌词扫描**：「精通」「全链路」等会被深度追问的词 → 标记并建议替换
4. **数据可答性**：逐个数值评估「面试能否答上来」→ 不能答的按 [ADR-0002](./docs/adr/0002-data-integrity-over-numbers.md) 降级
5. **学历一致性**：简历本科 vs 投递岗位要求 → 确保 config degree=本科
6. **薪资合理性**：背景 vs 期望 → 确保 config salary 与市场匹配

**输出格式**（展示给用户确认）：
```markdown
# 简历体检报告
## 🔴 必须修正
- [ ] 工作年限：「7年」→「5.5年」（可验证）
- [ ] 「精通」「全链路」→「主导」「深度参与」（面试陷阱）
## 🟡 建议修正
- [ ] 自然搜索流量「翻 N 倍」→「稳定增长」（不暴露基数）
- [ ] 关键词「排名第一」→「排名持续上升」（避免具体名次反向暴露）
## ⚠️ 需你补充真实信息
- [ ] 自由职业期（2025.03-至今）的客户数、服务领域、时段
## ✅ 可保留
- 注册转化率显著提升（加行业基准衬拖，不写具体百分比）
```

**关键约束**：体检报告未确认前，F1.5 不得运行，F4 定制不得使用未体检的 master.json。

### 4.1.5 简历画像分析与方向推荐模块（F1.5）★新增 `[决策 2026-07-07]`

**功能描述**：LLM 分析 master.json，推断求职者适合的行业/岗位方向/关键词，生成推荐的 target 列表，经用户确认后写入 config。**这是「全自动」与「方向正确性」之间的安全阀。**

| 项 | 说明 |
| --- | --- |
| 触发时机 | 简历解析完成（F1 出 master.json）后、岗位搜索（F2）之前 |
| 执行者 | zcode agent + LLM（强模型） |
| 输入 | master.json |
| 输出 | 推荐 target 列表（行业、岗位方向、搜索关键词、硬技能 per_job_keywords、建议薪资区间） |
| 暂停点 | ⏸️ 输出后暂停，等用户确认/修改/增删，确认后才进入 F2 |

**推荐 target 的输出格式**（展示给用户确认）：
```yaml
# LLM 推荐的求职方向（请确认/修改后继续）
recommended_targets:
  - name: "Python 后端"
    industries: ["互联网", "人工智能"]
    keywords: ["Python 后端", "Python 开发"]
    per_job_keywords: ["FastAPI", "PostgreSQL", "Redis"]
    suggested_salary: "20-30K"   # 基于履历年限推断
    reason: "履历中 Python 项目占比 70%，FastAPI 经验突出"
  - name: "数据工程师"
    industries: ["互联网", "金融科技"]
    keywords: ["数据开发", "ETL"]
    per_job_keywords: ["Spark", "Airflow"]
    reason: "有 2 个大数据项目，技能栈匹配"
# 应用城市：武汉、上海（每个方向生成 2 套 target）
```

**F1.5 必须遵守的 HRBP 硬约束**（v1.2 新增）：
- **主方向锁定为「SEO 增长」**，不得推荐「新媒体运营」为主方向（红海+竞争力弱）
- **子方向候选**（用户已确认 2 个，按转化潜力排序）：
  1. SEO/网站运营（最强护城河，优先分配额度）
  2. 内容运营（有开发者社区内容矩阵真实素材）
- 城市：武汉（10-15K）+ 上海（15-22K）
- 学历筛选：本科
- **必须基于 F1.6 通过的 master.json**，体检未确认不运行

### 4.2 岗位搜索模块（F2）

**功能描述**：按用户配置搜索 BOSS 直聘岗位，拉取 JD 详情，去重入库。

| 项 | 说明 |
| --- | --- |
| 工具 | **boss-cli**（jackwener/boss-cli，747★，原生 zcode skill，JSON 输出）`[调研结论]` |
| 搜索维度 | 关键词、城市、薪资范围、经验要求、学历、行业、公司规模（均可在 config 配置） |
| 多求职方向 | target 由 F1.5 LLM 推荐生成（不手填），每个方向独立搜索参数 |
| 城市 | 武汉 + 上海 `[决策 2026-07-07]`，每个方向×每个城市 = 1 套 target |
| 去重 | 按 job_id 去重；可选「同公司只投一岗」（dedupe_by_company） |
| 黑名单 | 支持排除指定公司（exclude_companies） |

### 4.3 JD 分析与匹配模块（F3）

**功能描述**：LLM 分析每个岗位的 JD，提取核心技能要求，与履历母库计算匹配度，过滤不匹配岗位。

| 项 | 说明 |
| --- | --- |
| 执行者 | **zcode agent + LLM**（语义理解任务）`[决策 2026-07-06]` |
| 输出 | JD 技能列表、匹配分数（0-1）、是否值得投递 |
| 硬过滤 | config 中的 `per_job_keywords`（如必须含「FastAPI」）必须命中才投 |
| 模型 | 便宜模型（如 haiku）做结构化抽取，省成本 |

### 4.4 简历定制模块（F4）⭐ 核心价值

**功能描述**：以「HR 专家 + 岗位业务专家」双视角，针对每个目标岗位调整简历的布局、信息呈现、关键词命中。

| 项 | 说明 |
| --- | --- |
| 执行者 | **zcode agent + LLM**（强模型，如 sonnet）`[决策 2026-07-06]` |
| 输入 | 目标岗位 JD + master.json 履历母库 |
| 输出 | 针对该岗位定制的 resume.yaml（RenderCV 格式） |
| **定制维度** | ① 从母库**选材**（哪些项目/经历放进这份简历）② **重排**（顺序按 JD 优先级）③ **改写**（bullet points 命中 JD 术语）④ **关键词对齐**（技能排序贴合 JD） |
| **润色策略** | **允许适度润色** `[决策 2026-07-07]`：不伪造经历/项目/学历，但对真实经历允许用更强的动词、量化数据、贴近 JD 术语。**核心约束**：所有数值必须满足 [ADR-0002](./docs/adr/0002-data-integrity-over-numbers.md) 三条规则（可信+可验证+有基数参照） |
| **数据表述标准**（master.json 必须使用，F4 不得回退） | 见 [CONTEXT.md](./CONTEXT.md)「数据表述标准模板」。已锁定的 4 条：①「翻 N 倍」→「稳定增长」②「排名第一」→「排名持续上升」③「注册转化率 X%」→「注册转化率显著提升」④「获客线索数」→「协同销售搭建线索流转闭环」 |
| **前置依赖** | **必须基于 F1.6 体检通过的 master.json**，体检未确认不得定制 |
| **黑名单（禁止）** | 凭空捏造项目、虚构任职公司、伪造学历、夸大任职年限、**凭经验编造「不夸张」的数值** |
| **红名单（鼓励）** | 强动词（主导/重构/优化/设计）、真实可验证的数据、JD 原词术语对齐、突出与岗位匹配的项目 |

**降本策略（50 岗 ≠ 50 次 LLM 调用）**：
1. **master.json 母库**：LLM 只做「选材+重排+微调」，不生成全新内容，杜绝幻觉
2. **JD 聚类**（`tailor_strategy: cluster`）：同一求职方向内，JD 技能签名相同的岗位复用同一份定制结果
3. **llm_cache 表**：相似 JD 的定制结果缓存，命中则 0 token
4. **模型分级**：简历定制用 sonnet，打招呼话术用 haiku
5. **预估**：50 岗 + cluster ≈ 4 次 sonnet + 50 次 haiku ≈ **< 2 美元/跑批**

### 4.5 PDF 生成模块（F5）

**功能描述**：把定制的简历内容渲染成专业、ATS 友好、可投递的 PDF 简历。

| 项 | 说明 |
| --- | --- |
| 工具 | **brilliant-CV**（Typst 原生简历模板，最流行）`[决策 2026-07-07]` |
| 排版引擎 | **Typst**（Rust 编写，~30MB 单文件，`brew install typst`，Apple Silicon 原生 bottle，原生 CJK 支持）`[调研结论]` |
| 输入 | LLM 生成的 `.typ` 源文件（纯文本，LLM 友好） |
| 输出 | PDF（文字可选可搜索，ATS 友好）+ PNG（Typst 原生支持 `typst compile --format png`） |
| 中文字体 | **思源黑体**（Noto Sans CJK SC，OFL 许可可商用）`[决策 2026-07-07]`，通过 brilliant-CV 的 `[layout.fonts]` 配置 |
| 备选 | 若需更学术风可切 RenderCV v2（5 套自带模板）；若需纯中文版式可切 Chinese-Resume-in-Typst |

**为何选 brilliant-CV 而非 RenderCV？** `[决策 2026-07-07]`
- 用户选「设计优先」，brilliant-CV 设计感更强、模块化、ATS+AI 友好
- 同样基于 Typst，安装门槛和中文支持一致
- LLM 直接生成 `.typ` 文件（纯文本语法），比 RenderCV 的 YAML→Typst 两层转换更直接
- 仓库：[yunanwg/brilliant-CV](https://github.com/yunanwg/brilliant-CV)，v4.0.1 活跃维护

**为何不用用户提供的 guizang-ppt-skill？** `[调研结论]`
- guizang-ppt-skill（19.9K★）实际是生成**单文件 HTML 横向翻页 PPT**（电子杂志/瑞士风）
- 它**不做 PDF，也不做简历**，与简历场景完全不匹配
- → **从 skill 栈中移除**

### 4.6 PDF 转图片模块（F6）

**功能描述**：把 PDF 转成 resume.jpg/png，用于 BOSS 聊天发送。

| 项 | 说明 |
| --- | --- |
| 主路径 | **RenderCV 原生 PNG 输出**（可能根本不需要单独转）`[调研结论]` |
| 兜底 | **PyMuPDF (fitz)**（自带 wheel 零外部依赖，比 pdf2image 快 4.5×） |
| 参数 | DPI=200（清晰且 <1MB）、JPG quality=95（视觉无损） |
| 多页处理 | Boss 图片简历通常 1 页最佳；多页可用 Pillow 纵向拼接长图 |

### 4.7 自动发送模块（F7）⭐ 技术最复杂

**功能描述**：自动向 HR 发送打招呼语 + 图片简历。

#### 4.7.1 双通道架构（关键）
| 通道 | 工具 | 能力 | 说明 |
| --- | --- | --- | --- |
| **文字打招呼** | boss-cli `greet` | 发送文字消息 | boss-cli 原生支持 |
| **图片简历** | **DrissionPage**（浏览器自动化） | 上传图片到聊天窗口 | **boss-cli 发不了图片**（需 MQTT/Protobuf，CLI 未实现）`[调研结论]` |

#### 4.7.2 浏览器框架选型（明确否定 Playwright/Selenium）`[调研结论]`
| 框架 | 结论 | 理由 |
| --- | --- | --- |
| **DrissionPage（主）** | ✅ 主推 | 国产，针对中文网站反爬优化，浏览器/请求双模式，社区有大量 Boss 实战 |
| **nodriver（反检测备用）** | ✅ 备用 | 2026 反检测基准唯一全过 31 个 Cloudflare 门，Direct CDP 无端口 |
| ~~Playwright~~ | ❌ 否定 | 默认开 CDP 端口并发 `Runtime.enable` 信号，Boss 可探测 |
| ~~Selenium~~ | ❌ 否定 | `navigator.webdriver=true`、`window.cdc_*` 直接识别 |

#### 4.7.3 图片发送技术路径
- **定位隐藏的 `<input type="file">`**：Boss 聊天页工具栏「图片」按钮触发隐藏 input
- **DrissionPage `input()` 直接传路径**：`page.ele('css:input[type="file"][accept*="image"]').input('/abs/path/resume.jpg')`
- **兜底**：DataTransfer/FileReader 构造文件对象注入
- **顺序约束**：先发文字 greet → **等待沟通关系建立** → 再发图片（get_jobs 逻辑）
- **图片规格**：1080px 宽、<1MB、JPG quality=95
- **绝不走接口**：Boss 上传走带签名的 multipart + WebSocket，签名涉及 `__zp_stoken__` 动态加密，逆向后极易封号

#### 4.7.4 Boss 防检测（基于 get_jobs Discussion #250 三步法）`[调研结论]`

**Boss 的检测手段**：
1. `disable-devtool` 库综合检测
2. `console.table` 时间差检测（DevTools 开启渲染慢）
3. `performance.now()` 时间差检测（debugger 命中慢）
4. `Function.prototype.toString` 完整性检测（hook 后会暴露）
5. CDP 远程调试端口探测
6. `navigator.webdriver` 等自动化指纹

**三步法绕过**：
1. **用真实 Chrome + 持久化 profile**（复用一次扫码登录的 session，避免重复登录）
2. **注入 stealth.js**（document_start 时机）：hook console.table/performance.now、伪造 Function.toString 完整性、清除 webdriver 指纹
3. **不暴露 CDP 端口**：DrissionPage 端口随机化；高风控期切 nodriver（Direct CDP 无端口）

#### 4.7.4.1 浏览器 profile 策略 `[v1.2 修正]`

**用户最终决策（v1.2 grill 修正）**：**Boss 专用 profile**（不复用日常 Chrome）。

> 🔄 **v1.1 → v1.2 决策变更**：
> - v1.1 原决策：复用日常 Chrome Default profile
> - v1.2 grill 拷问 9 后修正为：专用 profile
> - **修正原因**：复用日常 profile 有三个硬伤——①日常 Chrome 运行时项目无法启动（单 profile 不允许多进程）②被风控会影响日常 cookies ③日常多账号会混淆。专用 profile 更稳。

**配置**：
```yaml
sender:
  user_data_dir: "~/.boss-auto-apply/chrome-profile"  # Boss 专用，首次扫码后复用
```

**首次登录流程**：启动 → 人工扫码 → 登录态写入专用 profile → 后续复用。日常 Chrome 不受影响。

#### 4.7.5 限流与风控熔断（账号安全第一）`[决策：用主号]`
| 参数 | 最终值 | 理由 |
| --- | --- | --- |
| 每日总投递上限 | **15 次/天** `[决策 2026-07-07]` | 主号从严，Boss 软上限 ~100，留足 6 倍安全余量 |
| 单次间隔 | 随机 45-120 秒（正态分布，均值 70s） | 固定间隔是机器人铁证 |
| 连投限制 | 连投 5-8 次后强制休息 15-30 分钟 | 避免长时间高频窗口 |
| 活跃时段 | **仅工作日白天 09:00-18:00** `[决策 2026-07-07]` | 最人类化，风控风险最低 |
| 避开时段 | 18:00 后、凌晨、周末全天全停 | 夜间/周末投递是异常信号 |
| 账号预热 | 第1天≤5、第2天≤10、第3天起≤15，逐日递增 | 主号也要温和启动 |

**容量预估**：工作日白天 9 小时窗口，15 次/天 × 5 工作日 = **75 次/周**，单方向约 2-3 周可覆盖主流岗位。

**风控熔断（CircuitBreaker）**：
| 信号 | 动作 |
| --- | --- |
| 连续失败 ≥3 次 | 暂停 30 分钟（soft） |
| 滑动窗口失败率 >30% | 暂停 2 小时 + 告警（soft） |
| 出现验证码/人机验证 | **硬熔断 24h + 人工介入**（hard） |
| 「操作频繁/账号被限制」提示 | **硬熔断 24h + 告警**（hard） |
| 登录态丢失 | 停机 + 提示重新扫码 |

### 4.7.6 打招呼话术策略（F7 子模块）`[v1.2 新增，决策 2026-07-07]`

**功能描述**：LLM 生成的打招呼话术必须遵守的格式与禁含规则。

**话术结构**（HRBP 验证的最优模板）：
```
[岗位针对性开场，1句] + [你的核心亮点，1-2句，与JD强相关] + [可面试时间]
```

**示例**：
> 您好，看到贵司 SEO 岗位。我有 5.5 年 SaaS 产品 SEO 增长经验，曾主导产品官网 SEO 体系从 0 搭建，自然搜索流量稳定增长，核心关键词排名持续上升。本周可面试。

**禁含内容**（HRBP 强烈反对）：
- ❌ **薪资期望**：起步谈薪资让 HR 觉得「只看钱」，低于他们预算直接跳过你
- ❌ **空窗期解释**：自曝其短，HR 还没问就提，留下负面第一印象
- ❌ **学历说明**：除非 JD 明确要求，否则不主动提学历
- ❌ **通用模板话术**（如「您好我对这个岗位很感兴趣」）：HR 每天收几十条，转化率极低

**长度约束**：Boss 打招呼约 30 字以内（超长被截断），需精简到 1-2 句话。

### 4.8 状态管理与断点续传模块（F8）

**功能描述**：持久化每个岗位的处理状态，支持中断后从断点续跑。

| 项 | 说明 |
| --- | --- |
| 存储 | **SQLite**（单文件、零部署、事务、断点续传天然支持）`[调研结论]` |
| 状态机 | 9 态：`found → jd_analyzed → resume_tailored → pdf_generated → image_ready → greeted → image_sent`（成功路径）+ `skipped`（业务跳过）+ `failed`（失败） |
| 断点续传 | 失败时**留在原状态**（不前进），下次 `resume` 自然重试该步；只有副作用步骤（greet/发图）真正受 dry_run 控制 |
| 幂等 | 所有操作以 job_id 为主键，重复执行无副作用 |
| **增量监控** | `[v1.2 新增]` 投完一个城市岗位池后（验证发现 2-4 天可能投完），用 boss-cli 的 `watch` 命令每天检查新上架岗位，有新岗才投，无岗则休眠。Boss 每天都有新岗位 |

**失败重试分类**：
| 类别 | 判断 | 处理 |
| --- | --- | --- |
| network（重试） | 超时/连接重置 | retry_count+1，指数退避，留原态 |
| business（跳过） | 岗位下线/不匹配/黑名单 | status=skipped，永久不再处理 |
| risk（熔断） | 验证码/登录失效/限流提示 | 触发 CircuitBreaker，全局冷却 |
| llm（重试） | API 限流/超时 | 单独退避，可降级模型重试 |

### 4.9 配置系统模块（F9）

**功能描述**：用户通过 YAML 配置岗位关键词、地区、薪资、限流等所有可调参数。

**完整 config.yaml 结构**（节选，详见附录 A）：
```yaml
targets:                          # 求职方向（数组，可多个）
  - name: "后端开发-Python"
    keywords: ["Python 后端", "Python 开发"]
    city: "杭州"
    salary: "20-30K"              # 必须用 boss-cli 枚举值
    experience: "3-5年"
    degree: "本科"
    per_job_keywords: ["FastAPI", "PostgreSQL"]  # JD 必须命中才投
    daily_limit: 8

limits:                           # 全局限流
  daily_total: 15
  per_session: 10
  min_interval_sec: 30
  max_interval_sec: 120
  active_hours: [9, 22]
  cooldown_on_risk_sec: 1800

llm:
  model: "claude-sonnet"          # 简历定制用强模型
  greeter_model: "claude-haiku"   # 话术用便宜模型
  tailor_strategy: "cluster"      # 按方向聚类复用

pipeline:
  skip_greeted: true
  dedupe_by_company: true
  exclude_companies: []

sender:
  primary: "drissionpage"
  stealth: true
  headless: false                 # 有头更不易被检测

paths:
  resume_input: "input/resume.pdf"
  master_json: "data/master.json"
```

**校验**：JSON Schema 启动期校验，配置错误立即报错（如 per_session > daily_total）。

**敏感信息**：LLM API key、cookie 路径放 `~/.config/boss-auto-apply/secrets.yaml` 或环境变量，**绝不进 skill 目录**。

### 4.10 模式切换与降级模块（F10）

**功能描述**：支持三种运行模式，可在风控加剧时自动/手动降级。

| 模式 | 行为 | 适用场景 |
| --- | --- | --- |
| `auto` | 全自动，无人值守，限流跑 | 默认模式 `[决策：全自动但有严格限流]` |
| `confirm`（降级） | 每条投递前终端确认，人工点「发送」才执行 | 风控期 / 首次试跑 |
| `manual`（纯辅助） | 仅自动筛选+排序+生成话术，投递全人工 | 风控严竣期 |

**dry-run 模式**：只读步骤（解析/搜索/定制/生成）照常执行，副作用步骤（greet/发图）只打印不发送，但推进状态——**用于验证简历产物质量后再正式跑**。

**自动降级**：CircuitBreaker 24h 内硬熔断 ≥2 次，自动把 mode 切到 `confirm` 并告警。

---

## 5. 非功能需求

### 5.1 安全性 `[调研结论]`
- **Skill 安装门禁**：所有第三方 skill 必须通过 **SkillSpector**（NVIDIA，68 个漏洞模式）扫描，分数 ≥ 阈值才安装
- **trust_registry**：SQLite 记录每个已装 skill 的 hash/版本/扫描分/扫描日期；启动期校验 hash 未变
- **网络白名单**（建议）：浏览器自动化在沙箱/容器跑，仅放行 zhipin.com + LLM API 域名
- **Cookie 隔离**：boss-cli 涉及账号 cookie，重点审查网络请求目标是否全部指向 zhipin.com
- **重点扫描**：boss-cli（高危，涉及账号）、本项目 sender.py（涉及自动化）

### 5.2 可靠性
- 所有外部调用（boss-cli、LLM、RenderCV、浏览器）失败有重试 + 熔断
- 状态机保证断点续传，中断后 `resume` 不重头
- dry-run 模式先验证再正式跑

### 5.3 可维护性
- **Boss 风控是持续攻防战**：stealth.js、DOM 选择器需持续跟进 get_jobs Discussion #250 等
- DOM 选择器集中管理（`boss/selectors.py`），Boss 改版只改这里
- LLM 调用统一封装（`llm.py`），模型切换只改配置

### 5.4 性能
- 单次跑批 50 岗预估 LLM 成本 < 2 美元
- RenderCV Typst 引擎秒级编译
- DrissionPage 启动快（复用 profile）

### 5.5 可观测性
- 完整日志（loguru），含每次投递的成功/失败/风控信号
- run_log 表记录每次运行的计划/成功/失败/跳过数
- daily_quota 表跟踪每日投递计数

---

## 6. 技术架构

### 6.1 项目形态 `[决策：zcode skill + Python 为基座]`
**混合形态**：zcode skill 做「编排入口 + 调试交互」，Python 做「无人值守主控 + 确定性执行」。

### 6.2 LLM 调用方式 `[决策：全走 zcode agent]`
- **LLM 步骤（解析整理、JD 分析、简历定制、打招呼话术）全部通过 zcode agent 调用**
- 确定性步骤（搜索、生成、转图、发送、限流）用 Python 脚本
- ⚠️ **注意**：subagent 原推荐 SDK 直调（省 token），但用户选择全走 zcode agent。需在实现时注意：
  - agent 编排的稳定性（单步失败处理）
  - token 成本（通过 master.json 母库 + JD 聚类 + 缓存 + 模型分级控制）
  - 限流由 Python 主控强制执行（不交给 agent 决策）

### 6.3 编排模式
- **调试期**：`/skill boss-auto-apply` 触发 SKILL.md，zcode agent 交互式走单岗流程，便于观察简历定制质量
- **无人值守期**：`python main.py run`，Python 主控按状态机推进，LLM 步骤通过 subprocess 调 zcode agent（保留用户「全走 zcode agent」的决策）

### 6.4 目录结构（推荐）
```
boss-auto-apply/
├── SKILL.md                       # 薄编排入口，<300 行
├── references/                    # 渐进披露文档
│   ├── state-machine.md
│   ├── config-reference.md
│   └── troubleshooting.md
├── scripts/
│   ├── requirements.txt
│   └── boss_auto_apply/           # Python 包
│       ├── main.py                # CLI 入口：run / resume / status / dry-run
│       ├── pipeline.py            # 状态机驱动的主流程
│       ├── config.py              # 加载 + JSON Schema 校验
│       ├── llm.py                 # 统一封装 subprocess 调 zcode agent
│       ├── db.py                  # SQLite 状态库
│       ├── ratelimiter.py         # 限流 + 熔断
│       └── core/
│           ├── parser.py          # Docling
│           ├── searcher.py        # subprocess 调 boss-cli
│           ├── tailor.py          # 简历定制（调 llm.py）
│           ├── greeter.py         # 打招呼话术
│           ├── generator.py       # RenderCV CLI
│           ├── imager.py          # PyMuPDF
│           └── sender.py          # DrissionPage
├── config/
│   ├── config.example.yaml
│   └── config.schema.json
├── assets/resume/                 # RenderCV 模板
├── fonts/                         # 思源中文字体
├── driver/
│   ├── drission_runner.py
│   ├── nodriver_runner.py
│   └── stealth.js
├── input/
│   └── resume.pdf                 # 用户固定位置放简历
└── data/                          # 运行时产物（.gitignore）
    ├── jobs.db
    ├── resumes/                   # {job_id}.pdf / {job_id}.png
    ├── master.json
    ├── cache/
    └── logs/
```

### 6.5 数据库表结构
```sql
-- jobs: 每个候选岗位一行，状态机主表
CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY,
  target_name TEXT NOT NULL,
  keyword TEXT, city TEXT, title TEXT, company TEXT,
  salary TEXT, experience TEXT, degree TEXT,
  jd_full TEXT, skills_required TEXT,   -- JSON array
  match_score REAL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'found', -- 9 态枚举
  tailored_resume_path TEXT,
  resume_pdf_path TEXT,
  resume_image_path TEXT,
  tailored_greet TEXT,
  greet_sent_at TEXT,
  image_sent_at TEXT,
  error_msg TEXT, error_category TEXT,
  retry_count INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT
);

-- run_log: 每次运行审计
CREATE TABLE run_log (
  run_id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT,
  mode TEXT, target_name TEXT,
  planned INTEGER, succeeded INTEGER, failed INTEGER, skipped INTEGER
);

-- daily_quota: 每日限额（跨多次运行）
CREATE TABLE daily_quota (
  date_key TEXT PRIMARY KEY,
  sent_count INTEGER DEFAULT 0,
  risk_events INTEGER DEFAULT 0,
  cooldown_until TEXT
);

-- llm_cache: JD→定制结果缓存（降本）
CREATE TABLE llm_cache (
  cache_key TEXT PRIMARY KEY,
  target_name TEXT, jd_signature TEXT,
  response TEXT, model TEXT,
  tokens_in INTEGER, tokens_out INTEGER,
  created_at TEXT
);
```

### 6.6 状态机
```
                         ┌──────── 风控/限流 ────────┐
                         ▼                           │
  found ──▶ jd_analyzed ──▶ resume_tailored ──▶ pdf_generated ──▶ image_ready ──▶ greeted ──▶ image_sent
   │           │                 │                  │                 │              │
   │           │(不匹配/黑名单)   │(LLM失败)         │(RenderCV失败)   │(发送失败)    │(发送失败)
   │           ▼                 └────── 都可转 ─────┴────────────────┴──────────────┴─▶ failed
   │        skipped                                                                      │
   │                                                                                     │ 重试超限
   └──────────────────────────────────────────────────────────────────────────────────▶ skipped
```

---

## 7. 用户提供的 4 个 skill 评估结论 `[调研结论]`

| Skill | 实际能力 | 本项目用途 | 评估 |
| --- | --- | --- | --- |
| **markitdown** (microsoft) | 文件转 Markdown，但 PDF 是短板 | 降级为 Office 简历兜底解析器 | ⚠️ 不够用，主解析器换 **Docling** |
| **boss-cli** (jackwener) | 搜索/详情/greet（文字），**不能发图片** | 岗位搜索 + 打招呼（文字通道） | ✅ 搜索够用；❌ 发图片需另配 DrissionPage |
| **guizang-ppt-skill** (op7418) | 生成 HTML 横向 PPT，不做 PDF 不做简历 | **无** | ❌ **完全不匹配，从 skill 栈移除** |
| **SkillSpector** (NVIDIA) | AI skill 安全扫描，68 漏洞模式 | skill 安装门禁 | ✅ 完美满足安全要求 |

### 7.1 新增/替换的 skill 清单
| 环节 | 工具 | 替换/新增 | 理由 |
| --- | --- | --- | --- |
| 简历解析 | **Docling** | 替换 markitdown | PDF 基准第一，中文/表格强 |
| 简历画像分析 | **zcode agent + LLM** | 新增（F1.5） | 动态推断求职方向 |
| PDF 生成 | **brilliant-CV（Typst 原生）** | 替换 guizang-ppt-skill | 设计优先，LLM 直接生成 .typ |
| PDF 转图片 | **Typst 原生 PNG + PyMuPDF 兜底** | 新增 | Typst 原生支持 PNG 输出 |
| 浏览器发送 | **DrissionPage + nodriver** | 新增（boss-cli 不支持发图） | 反检测 + 中文站优化 |
| 参考项目 | **get_jobs** (loks666) | 仅参考思路，**不直接用代码** | 许可证禁止商业化，个人自用也仅限参考 |

### 7.2 安全检测流程
所有 skill（含用户提供的 + 新增的）安装前必须：
1. 下载到隔离沙箱
2. SkillSpector 扫描（输出 0-100 分 + 漏洞清单）
3. 分数 ≥ 70 且无 Critical → 安装 + 写 trust_registry
4. 分数 < 60 或有 Critical → 拒绝

---

## 8. 风险评估与缓解

### 8.1 技术风险
| 风险 | 等级 | 缓解 |
| --- | --- | --- |
| **Boss 风控封号** | 🔴 高 | 严格限流 + 风控熔断 + stealth.js + 主号温和启动；用主号意味着更保守的参数 |
| **Boss 防检测失效** | 🔴 高 | 持续跟进 get_jobs #250；预留 mode 切换快速降级到半自动 |
| **boss-cli 逆向 API 失效** | 🟡 中 | boss-cli 是第三方，依赖作者更新；做好模块隔离便于替换 |
| **RenderCV 模板不够美观** | 🟡 中 | 预留升级路径到 brilliant-CV（Typst 原生） |
| **全走 zcode agent 的稳定性** | 🟡 中 | token 成本控制（master.json + 聚类 + 缓存）；Python 主控强制限流，不交给 agent |

### 8.2 合规与法律风险
| 风险 | 等级 | 说明 |
| --- | --- | --- |
| **违反 BOSS 用户协议** | 🔴 高 | 自动化投递明确违规；纯个人自用不改变违规性质，仅降低被追责概率 |
| **get_jobs 许可证** | 🟢 低 | 仅参考思路，不直接用代码；个人自用不涉及商业化 |
| **账号封禁** | 🟡 中 | 主号被风控可能影响求职；已通过限流+熔断+降级降低概率，但无法消除 |

---

## 9. 里程碑（建议）

| 阶段 | 目标 | 交付物 |
| --- | --- | --- |
| **M1: MVP 骨架** | 跑通单岗全流程（dry-run） | 目录结构 + config + SQLite + 状态机 + Docling 解析 + RenderCV 生成 |
| **M2: 搜索+定制** | 多岗搜索 + LLM 定制 + boss-cli greet | searcher + tailor + greeter + master.json 降本 |
| **M3: 自动发送** | DrissionPage 发图片 + 防检测 | sender + stealth.js + 登录态复用 |
| **M4: 限流+风控** | 无人值守安全跑批 | ratelimiter + circuit_breaker + daily_quota |
| **M5: 安全门禁** | SkillSpector 集成 | safe_skill_install + trust_registry |

---

## 10. 决策固化 ✅

### v1.1 决策（10 项，详见附录 D）
| # | 决策点 | 最终值 | v1.2 是否修正 |
| --- | --- | --- | --- |
| 1 | 求职城市 | 武汉 + 上海 | ✅ 保持 |
| 2 | 求职方向 | LLM 推荐后人工确认 | 🔧 v1.2 加 HRBP 约束（主方向锁定流量增长/SEO） |
| 3 | 每日上限 | 15 次/天 | ✅ 保持 |
| 4 | 中文字体 | 思源黑体 | ✅ 保持 |
| 5 | PDF 模板 | brilliant-CV | ✅ 保持 |
| 6 | 多城市投递 | 双城市同方向 | ✅ 保持 |
| 7 | 浏览器 profile | ~~复用日常 Chrome~~ | 🔧 **v1.2 修正为 Boss 专用 profile** |
| 8 | 投递时段 | 工作日白天 09:00-18:00 | ✅ 保持 |
| 9 | 方向确认 | 人工确认后执行 | ✅ 保持（并扩展到 F1.6 体检确认） |
| 10 | 话术约束 | 允许适度润色 | 🔧 v1.2 加 ADR-0002 数据诚信规则 |

### v1.2 新增决策（9 轮 grill，HRBP + PM 视角）
| # | 决策点 | 最终值 | 影响章节 |
| --- | --- | --- | --- |
| G1 | 工作年限 | **5.5 年**（修正简历「7年」） | F1.6, master.json |
| G2 | 空窗期 | 自由职业 B+C 策略（3+ 客户） | F1.6, master.json |
| G3 | 学历筛选 | **本科**（不投要求硕士及以上的岗） | F2, config |
| G4 | 薪资带 | 上海 15-22K / 武汉 10-15K | config, F2 |
| G5 | 核心方向 | **SEO 增长**（聚焦 SaaS/工具类赛道） | F1.5 约束 |
| G6 | 数据诚信 | 4 条标准表述（翻N倍→稳定增长 等） | F1.6, F4, ADR-0002 |
| G7 | 子方向 | SEO + 内容运营（权重 0.6/0.4） | F1.5, config |
| G8 | 打招呼话术 | 亮点开场+面试时间，禁含薪资/空窗/学历 | F7.6 |
| G9 | 投完后策略 | 增量监控新岗（boss-cli watch） | F8 |

> ✅ 无剩余待确认项。PRD v1.2 经 PM + HRBP 双视角审阅，可进入开发阶段。

---

## 附录 A：完整 config.yaml 模板

```yaml
# ============================================================
# BOSS 直聘智能投递助手 - 配置文件（v1.1，已纳入用户决策）
# ============================================================

# ============ 求职城市（HRBP 决策 2026-07-07：武汉+上海）============
cities: ["武汉", "上海"]

# ============ 求职方向（v1.2：3 子方向，HRBP + 用户共同决策）============
targets: []                          # 留空，由 F1.5 推荐 + F1.6 体检后填充
target_constraints:
  primary_direction: "SEO 增长"      # HRBP 硬约束：主方向锁定
  sub_directions:                    # 2 个子方向，按转化潜力排序
    - name: "SEO/网站运营"
      weight: 0.6                    # 额度分配权重
      keywords_hint: ["SEO", "SEO优化", "网站运营", "搜索引擎优化"]
    - name: "内容运营"
      weight: 0.4
      keywords_hint: ["内容运营", "社区运营", "技术博客"]
  salary:                            # HRBP 务实薪资带，按城市分档
    武汉: "10-15K"
    上海: "15-22K"
  experience: "3-5年"
  degree: "本科"                     # HRBP 决策：只投本科友好岗位
  require_confirmation: true

# ============ 全局限流（决策 2026-07-07：主号15/天+工作日白天）============
limits:
  daily_total: 15                    # 决策：15次/天（主号从严）
  per_session: 10
  min_interval_sec: 45               # 单次间隔下限
  max_interval_sec: 120              # 单次间隔上限（随机抖动）
  burst_size: 8                      # 连投上限
  burst_rest_sec: [900, 1800]        # 连投后休息区间
  active_hours: [9, 18]              # 决策：仅工作日白天
  active_weekdays_only: true         # 决策：周末停
  cooldown_on_risk_sec: 1800
  warmup_schedule: [5, 10, 15]       # 第1/2/3+天的预热上限

# ============ 调度 ============
schedule:
  mode: "manual"                     # manual / cron
  cron: "0 10,14 * * 1-5"            # 工作日 10/14 点（mode=cron 时）

# ============ LLM（决策 2026-07-07：全走 zcode agent）============
llm:
  model: "claude-sonnet-4-5"         # 简历定制（强模型）
  profiler_model: "claude-sonnet-4-5" # F1.5 简历画像分析（需强模型）
  greeter_model: "claude-haiku"      # 话术（便宜模型）
  jd_model: "claude-haiku"           # JD 技能提取
  temperature: 0.3
  tailor_strategy: "cluster"         # 按方向聚类复用
  cache: true
  polish_level: "moderate"           # 决策：允许适度润色（红黑名单见 F4）

# ============ 流程开关 ============
pipeline:
  mode: "auto"                       # auto / confirm / manual
  dry_run: false
  skip_greeted: true
  dedupe_by_company: true
  exclude_companies: []
  require_jd_match: true
  require_target_confirmation: true  # 决策：F1.5 暂停等人确认

# ============ 浏览器发送（v1.2 修正：专用 profile）============
sender:
  primary: "drissionpage"
  user_data_dir: "~/.boss-auto-apply/chrome-profile"  # v1.2 修正：Boss 专用，不复用日常
  stealth: true
  headless: false
  check_chrome_running: true         # 启动前检测 Chrome 是否被占用
  send_image_resume: true
  wait_relation_sec: 60              # 打招呼后等待沟通关系建立

# ============ PDF 生成（决策 2026-07-07：brilliant-CV + 思源黑体）============
pdf:
  template: "brilliant-cv"           # 决策：brilliant-CV
  font: "Noto Sans CJK SC"           # 决策：思源黑体
  font_path: "fonts/"                # 字体文件目录

# ============ 路径 ============
paths:
  resume_input: "input/resume.pdf"
  master_json: "data/master.json"
  resumes_out: "data/resumes/"
  db: "data/jobs.db"
  logs: "data/logs/"
```

---

## 附录 B：一键安装依赖清单

```bash
# 排版引擎（~30MB，Apple Silicon 原生）
brew install typst

# Python 依赖
pip install "DrissionPage>=4.1.0"     # 浏览器主驱动
pip install "nodriver>=0.4"           # 反检测备用
pip install pymupdf                   # PDF→图片兜底
pip install docling                   # 简历解析（替代 markitdown）
pip install pyyaml jsonschema loguru pydantic pillow

# 中文字体（思源黑体，决策 2026-07-07）
brew install --cask font-noto-sans-cjk-sc

# brilliant-CV 模板（决策 2026-07-07）
git clone https://github.com/yunanwg/brilliant-CV.git templates/brilliant-cv
```

---

## 附录 C：调研来源汇总

**核心参考项目：**
- [loks666/get_jobs](https://github.com/loks666/get_jobs) — 图片简历发送 + AI 话术思路（仅参考，许可证禁商业化）
- [get_jobs Discussion #250](https://github.com/loks666/get_jobs/discussions/250) — Boss 防检测权威讨论
- [jackwener/boss-cli](https://github.com/jackwener/boss-cli) — 岗位搜索 + greet（747★，原生 skill）
- [rendercv/rendercv](https://github.com/rendercv/rendercv) — PDF 生成（17K★，Typst 引擎）
- [docling-project/docling](https://github.com/docling-project/docling) — 简历解析（25K★）
- [NVIDIA/SkillSpector](https://github.com/nvidia/skillspector) — skill 安全扫描

**反检测技术：**
- [0xsdeo/AntiDebug_Breaker](https://github.com/0xsdeo/AntiDebug_Breaker) — 反调试 hook
- [geekgeekrun](https://github.com/geekgeekrun/geekgeekrun) — Boss 自动化参考
- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) — 反检测天花板
- [DrissionPage](https://drissionpage.cn/) — 中文站反爬优化

**Typst 简历模板：**
- [yunanwg/brilliant-CV](https://github.com/yunanwg/brilliant-CV) — 升级路径
- [OrangeX4/Chinese-Resume-in-Typst](https://github.com/OrangeX4/Chinese-Resume-in-Typst) — 中文简历专用

---

## 附录 D：用户决策固化表（v1.1 新增）

本附录完整记录两轮共 18 次求证的用户决策，作为开发的最终依据。

### D.1 战略决策（2026-07-06，4 项）
| # | 决策点 | 用户选择 | 对方案的影响 |
| --- | --- | --- | --- |
| S1 | 发送方式 | 打招呼 + 发送图片简历附件（参考 get_jobs） | 引入 DrissionPage 浏览器自动化 |
| S2 | 简历输出 | 生成新 PDF 简历 | 引入 PDF 生成模块 |
| S3 | 定制粒度 | 每岗一份定制简历 | 流程核心，token 成本需控制 |
| S4 | PRF 含义 | 格式不重要，目标是简历可投递 | 明确 guizang-ppt-skill 不匹配 |

### D.2 方向决策（2026-07-06，3 项）
| # | 决策点 | 用户选择 | 对方案的影响 |
| --- | --- | --- | --- |
| D1 | 使用场景 | 纯个人自用 | get_jobs 许可证风险最低，可大胆参考 |
| D2 | 技术基座 | zcode skill + Python | 混合形态，skill 编排+Python 执行 |
| D3 | 自动化深度 | 全自动但有严格限流 | 限流+熔断+降级是核心 |

### D.3 实现细节决策（2026-07-06，4 项）
| # | 决策点 | 用户选择 | 对方案的影响 |
| --- | --- | --- | --- |
| I1 | Boss 账号 | 用主号 | 限流从严，预热递增 |
| I2 | 原简历位置 | 固定位置+文件名 | input/resume.pdf |
| I3 | LLM 调用 | 全走 zcode agent | llm.py 通过 subprocess 调 agent |
| I4 | PRD 详略 | 完整版 | 本文档 |

### D.4 待确认清单决策（2026-07-07，10 项）★ 本次新增
| # | 决策点 | 用户选择 | 对方案的影响 |
| --- | --- | --- | --- |
| C1 | 求职城市 | 武汉 + 上海 | 双城市，每方向×2 套 target |
| C2 | 求职方向 | LLM 分析简历后动态推荐 | **新增 F1.5 简历画像分析模块** |
| C3 | 每日上限 | 15 次/天（主号从严） | limits.daily_total=15 |
| C4 | 话术约束 | 允许适度润色 | F4 红黑名单词表 |
| C5 | PDF 模板 | brilliant-CV（设计优先） | **F5 技术路径变更：Typst 原生替代 RenderCV** |
| C6 | 双城方向 | 同方向（武汉、上海投相同方向） | 2×N 套 target |
| C7 | 中文字体 | 思源黑体 | font: Noto Sans CJK SC |
| C8 | 活跃时段 | 仅工作日白天 09:00-18:00 | active_hours + active_weekdays_only |
| C9 | 方向确认 | 人工确认后执行 | **F1.5 暂停点 require_target_confirmation** |
| C10 | 浏览器 profile | 复用日常 Chrome Default | **新增 4.7.4.1 技术坑说明** |

### D.5 因决策而新增/变更的 PRD 章节
- ✅ 新增 **F1.5 简历画像分析与方向推荐模块**（因 C2、C9）
- ✅ 变更 **F5 PDF 生成模块**（从 RenderCV 改为 brilliant-CV，因 C5）
- ✅ 变更 **F4 简历定制模块**（新增红黑名单词表，因 C4）
- ✅ 新增 **4.7.4.1 浏览器 profile 策略**（含技术坑说明，因 C10）
- ✅ 变更 **F2 岗位搜索模块**（target 动态生成，因 C2）
- ✅ 变更 **4.7.5 限流参数**（工作日白天+主号15/天，因 C3、C8）
- ✅ 变更 **附录 A config.yaml**（适配所有决策）
- ✅ 变更 **附录 B 依赖清单**（移除 rendercv，新增 brilliant-CV clone）

---

**文档结束（v1.1）。所有需求已与用户对齐，无剩余待确认项，可进入 M1 开发阶段。**
