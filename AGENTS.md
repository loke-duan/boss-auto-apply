# AGENTS.md — AI Agent 项目指引

> 本文件是给 AI coding agent（ZCode / Claude Code 等）的项目约束总览。
> 所有 agent 在本项目中工作前**必须读完本文件**，并始终遵守其中的红线约束。

## 项目概述

**项目名称**：Boss直聘智能投递助手（`boss-auto-apply`）
**目标**：自动完成「理解简历 → 交互审查 → HRBP 体检 → 方向推荐 → 搜索岗位 → JD 匹配 → 简历定制 → PDF/PNG 生成 → 自动投递（打招呼+发图片简历）」全闭环
**开源说明**：候选人专属信息（姓名/手机/邮箱/方向/年限/学历/薪资）均从 `config.yaml` 和 `master.json` 动态读取，不在代码或文档中硬编码。

## 关键文档位置

| 文档 | 路径 | 说明 |
|---|---|---|
| README | `README.md` | 项目介绍 + 快速开始 + 配置说明 |
| 领域语言 | `CONTEXT.md` | 领域术语定义 + 红线约束 |
| 技术设计 | `docs/DESIGN.md` | M1+M2 技术设计（14 章） |
| 交接文档 | `docs/HANDOVER.md` | 当前进度 + 下一步（压缩恢复用） |
| ADR-0001 | `docs/adr/0001-hrbp-health-check-mandatory.md` | HRBP 体检强制 |
| ADR-0002 | `docs/adr/0002-data-integrity-over-numbers.md` | 数据诚信三规则 |
| ADR-0003 | `docs/adr/0003-nodriver-for-chat-page.md` | nodriver 用于聊天页发送（绕过 zpAegis） |
| ADR-0004 | `docs/adr/0004-multi-format-resume.md` | 多格式简历解析（pdf/docx/md/txt） |
| Agent 编排 | `SKILL.md` | zcode agent 交互式调试入口 |

## 技术栈

- **LLM**：Claude CLI（`claude -p --output-format json --model sonnet/haiku`）
- **浏览器自动化**（双引擎，ADR-0003）：
  - **DrissionPage 4.1.1.4**：登录 + 搜索 + 详情页操作（zpAegis 不拦截这些页面）
  - **nodriver**：聊天页发送话术 + 图片（绕过 zpAegis 反爬，不调 CDP `Runtime.enable`）
  - ⚠️ 两者**不能同时用同一 profile**（会互相覆写 cookies）
- **PDF 生成**：Typst + brilliant-CV v4（profile 目录结构）
- **简历解析**（多格式，ADR-0004）：docling（主）→ PyMuPDF/python-docx（兜底）→ TextRuleParser（md/txt）
- **Chrome**：专用 profile `~/.boss-auto-apply/chrome-profile`，端口 9559

## 核心约束（红线）

### 1. 体检是闸门（ADR-0001）
`master.health_check_status != 'passed'` 时，画像分析/简历定制**拒绝运行**。

### 2. 数据诚信三规则（ADR-0002）
所有量化数据必须**可信 + 可验证 + 有基数参照**三者同时满足，否则降级为定性描述。
禁止为「让简历显得有数据」而保留站不住脚的数值，也禁止凭经验编造「不夸张的数值」。

### 3. 候选人信息从 config/master 读取
- **工作年限**：从 `master.json` 的 `basics.work_years_total` 读取，LLM 不得改大
- **学历/方向/薪资**：从 `config.yaml` 的 `target_constraints` 读取
- **姓名/手机/邮箱**：从 `master.json` 的 `basics` 读取
- **禁忌词**：通用词（精通/100%/完美/极致）代码内置（`tailor.FORBIDDEN_PATTERNS`）；候选人专属词从 `config.forbidden_words_extra` 注入
- **业绩数据 = 定性描述**（不泄露商业数据）

### 4. 禁忌词（通用）
- **政策**（2026-07-16 调整）：禁忌词**只告警不阻断**。`validate_tailored` 返回 `(block_violations, warn_violations)`：
  - **阻断级**（直接抛 `LlmJsonParseError`，不重试）：幻觉项目（不在 master）、工作年限超限
  - **告警级**（只 log warning，不重试、不阻断）：通用/专属禁忌词
  - 历史教训：曾用「全链路」作阻断词 + LLM 重试，导致每个 job 多花 1 次 sonnet 调用（~2.5min）却收益甚微，已移除「全链路」并取消重试
- 建议改写：`精通 → 熟练掌握`、`完美/极致/100% → 删除或具体描述`

### 5. 禁伪造
项目 / 公司 / 学历 / 年限 / 数据**一律不可凭空生成**。简历定制只能从 `master.json` 选材重组。

### 6. 简历项目经历
保留**全部项目**（时间倒序），匹配的重点改写，不匹配的简化保留。不按 JD 匹配度排序。

### 7. 流式架构（采一个投一个）
`run` 命令走 `Pipeline.run_streaming`：搜到一个岗位 → 立即走 `fetch_detail → 匹配 → 定制 → PDF → PNG → 发送` 全链路 → 再搜下一个。**不要改回批量**（先搜完再处理）——批量模式搜索阶段耗时 2h+，体验差。

关键实现（`pipeline.py:_process_one`）：
- `pdf_generated → image_ready` 后**不 return**，fall-through 到 `image_ready → _step_send` 连续推进（同一次调用内完成发送）
- 真发送路径下，进 `_step_send` 前用 `report_result(success=False)` 回滚 `_process_one` 已做的预扣，避免与 sender 内部 acquire 双扣 `daily_quota.sent_count`
- dry-run 模式止于 `image_ready`（不进 sender）

## 浏览器引擎约束（ADR-0003）

| 操作 | 引擎 | 原因 |
|---|---|---|
| 登录（扫码） | DrissionPage | zpAegis 不拦截登录页 |
| 岗位搜索 | DrissionPage | zpAegis 不拦截搜索页 |
| 详情页操作 | DrissionPage | zpAegis 不拦截详情页 |
| **聊天页发送** | **nodriver** | zpAegis 检测 CDP `Runtime.enable` 阻断 zpToken，nodriver 不调该命令 |

**配置**：`config/config.yaml` 中 `sender.driver: "nodriver"` 切换聊天页发送引擎（真发送必须用 nodriver）。

**关键限制**：
- 用 nodriver 前必须 `pkill -9 -f "Google Chrome"`（避免与 DrissionPage profile 冲突）
- nodriver 是 async API，项目用 `asyncio.run` 桥接到同步接口（`NodriverChatSender`）
- 登录态用 DrissionPage 的 `python -m boss_auto_apply login` 建立，nodriver 复用 profile cookies

**NodriverChatSender 工程约束**（2026-07-17 实测沉淀，`boss_auto_apply/browser/nodriver_sender.py`）：
1. **预启动 Chrome**：nodriver 0.50.3 的 `uc.start` 自己启动 Chrome 后只等 2.75s 就判失败，但 macOS + 持久化 profile 启动需 3-5s。项目改为**先 subprocess 启动 Chrome 到空闲端口 + 轮询 `/json/version` 就绪（30s）+ nodriver 用 `host/port` 连接已启动实例**（`_launch_chrome_and_connect`）。
2. **sandbox=False + `--no-sandbox`**：`uc.start` 的 `sandbox=False` 参数对 Chrome 150 不生效，必须同时在 `browser_args` 加 `--no-sandbox`（`NodriverChatSender.__init__` 默认已处理）。
3. **contenteditable 输入用 `execCommand('insertText')`**：nodriver 的 `send_keys` 对 Boss 的 `.chat-input[contenteditable="true"]` **完全无效**（输入框仍空）。必须用 JS `document.execCommand('insertText', false, text)`（`_send_text_async`）。
4. **`evaluate` 返回值用 JSON.stringify 包装**：nodriver 0.50.3 的 `evaluate(return_by_value=True)` 返回 `RemoteObject`，`str(RemoteObject) ≠ JS 值`。**禁止用 `return_by_value=True`**，改用 `JSON.stringify(...)` + Python 端 `json.loads`（`_confirm_text` / `_confirm_image`）。
5. **详情页 greet 用 `/job_detail/{id}.html`**：`web_greeter._navigate_to_job` 用 `/job_detail/{job_id}.html`（与 `fetch_detail` 同款 URL），**不要用** `job-detail?securityId={job_id}`（job_id 不是 securityId，会 button_not_found）。
6. **「立即沟通」和「继续沟通」共用 class `.btn-startchat`**（2026-07-19 CDP 诊断实测）：只靠 button.text 区分。`web_greeter._read_btn_text` 读文本，"继续沟通" → already_friend=True 跳过 click，"立即沟通" → 正常 click。**禁止用 `.btn-continuechat`** selector（这个 class 在 Boss DOM 里不存在）。
7. **sender profile 互斥自动 quit**（2026-07-19 v2 修复）：`sender.send_application` 在 greet 成功后、调 nodriver 前**主动 quit DrissionPage Chrome**（`self.browser.quit()`），释放 profile 锁。否则 nodriver 启动会因 SingletonLock 冲突 30s 超时。当 status=greeted/text_sent 断点续传时，**不启动 DrissionPage**（`needs_drissionpage = (status == "image_ready") or dry_run`），直接交给 nodriver。
8. **URL 检测用 JS 读 `location.href`**（v2 修复）：DrissionPage `tab.url` 同步慢于 Vue Router push，greet 后跳转聊天页 10s 内 `tab.url` 仍读不到新 URL → click_timeout 误报。`web_greeter._read_url_js` / `_read_body_text` 用 `tab.run_js("location.href" / "document.body.innerText")` 直接读浏览器当前值。
9. **聊天图片发送用"精准 file input + 数量变化"双重判据**（2026-07-19 v2 修复，**重要**）：
   - **精准选 file input**：Boss 聊天页有 3 个 `input[type=file]`——聊天图片、简历附件、对话框。`_locate_chat_image_input` 只选 `accept` 纯图片格式（`image/gif,image/jpeg,...`）且**不含 `application/pdf`** 的那个；旧版 `file_inputs[0]` 在某些情况下选错（聊天页未完全渲染时只有简历附件 input）。
   - **基于图片数量增加判成功**：`_confirm_image_by_count` 在 send_file 前后用 `_count_chat_content_images` 计数聊天消息区的内容图片（`img[src*="bosszhipin.com/beijin"]` / `img[src*="chat/file"]` / `img[src*="imgaz.bosszhipin.com"]`），**只有数量真正增加才算成功**。
   - **历史 bug**：旧版 `_confirm_image` 只看"聊天区最后一张 img 的 naturalWidth>0"，但聊天区天然有历史图片（HR 早期发的、UI 占位图），**永远返回 True**——即使新图根本没上传也报成功（4210cdf1/382b946e 假阳性案例：`last_src='20221117/...'` 是 2022 年的旧图）。

## 方向推荐 v2（2026-07-19 重构，开源化）

**核心变化**：方向不再锁定，完全由 LLM 自主从 master 推导。

- **profiler.md prompt** 含「行业经验识别方法论」：从 master.projects/work_experiences/skill_modules 中识别核心行业（命中 ≥ 2 项目或累计 ≥ 5 年 → core 行业，weight 0.35-0.5）
- **profiler.validate_targets** 放宽：不强制 sub_direction ∈ config 池，只校验 weight 总和 ≈ 1.0、city 合法、(sub_direction, city) 不重复、sub_direction 非空
- **config sub_directions 降级为 keywords_hint**（可选池辅助 LLM，不强制）
- 删除 SEO/「投放运营」硬编码残留（`profiler.py:5-11,197-200`）

## 限流策略 v2（2026-07-19 重构）

**核心变化**：处理阶段不预扣 interval 名额，只在真发送时占名额。

- **`pipeline._process_one` 移除开头 `limiter.acquire`**（matcher/tailor/PDF/PNG 不占名额）
- **`pipeline.mark_skipped` 后不调 `report_result(success=False)`**（业务跳过不是失败，不应触熔断）
- **`sender.send_application` acquire 分级**：`wait_sec ≤ 180s`（频次类）→ sleep 重试一次；`> 180s`（熔断/日上限）→ raise
- **设计意图**：流式 N 个新岗位，每个岗位处理 5 分钟（tailor）天然满足 interval 要求；不再"第 2 个起全被 interval 拦"

## 个性化招呼 v2（2026-07-19 新增）

**核心变化**：替换 sender 的兜底默认话术。

- **`tailor.generate_greet_text`**（haiku，快+省）：根据 JD + master 强相关项目生成 2-3 句个性化招呼
- **集成点**：`pipeline.jd_analyzed → resume_tailored` transition 时调，落库字段 `tailored_greet`
- **失败兜底**：任何异常/禁忌词命中 → 返回 None，`validate_greet_before_send` 走兜底默认
- **prompt**：`prompts/greet.md`，含反例约束（禁用「精通/100%/完美/极致」、禁泄露商业数据、禁夸大工龄）

## 多格式简历解析（ADR-0004）

| 格式 | 解析器 | 降级链 |
|---|---|---|
| `.pdf` | DoclingParser → PymupdfRuleParser | docling 装不上时降级 |
| `.docx` | DoclingParser → PythonDocxParser | docling 装不上时降级 |
| `.md` / `.txt` | TextRuleParser | 纯 Python，无降级 |

`get_parser(ext=".pdf")` 按扩展名分发；`pipeline._step_parse` 和 `main._cmd_parse` 自动传 ext。

## 标准命令

```bash
# 预检（确认 claude/typst/字体/模板/简历就位）
python -m boss_auto_apply preflight

# 登录 Boss（首次/过期后，用 DrissionPage 扫码）
python -m boss_auto_apply login

# dry-run（不真发，验证全链路）
python -m boss_auto_apply --non-interactive dry-run

# 真发送（nodriver 引擎，会真发招呼+简历）
python -m boss_auto_apply run

# 查看状态
python -m boss_auto_apply status

# 断点续传
python -m boss_auto_apply --non-interactive resume

# 全测试
python -m pytest tests/ -q
```

## 工作原则

1. **做事情前先规划再执行**，不要像无头苍蝇一样乱转
2. **不要重复造轮子**——项目已有的方法（如 `check_login`、`preprocess_resume_image`）要复用
3. **有任何不确定的时候，不要猜测，而是向我直接求证**
4. **不要为了节省资源，而省略输出任何信息**
5. 代码风格匹配周围代码（注释密度、命名、惯用法）
6. 引用代码用 `file_path:line_number` 格式

## v2 第三轮关键约束（2026-07-19）

> ⚠️ **本段已被 v3 通用化重构取代（见下方 v3 段）**：
> - 「matcher 岗位类别硬过滤」→ v3 改为基于 candidate_profile.target_role_keywords 动态判断
> - 「删除 score>=0.7 强制 worth=True」→ v3 改为 worth 四要素（category + level + hard_filter + score）
> - 「pipeline title 白名单+黑名单过滤」→ v3 已删除（pipeline 不再过滤 title，全交给 matcher）
>
> 本段保留作为历史背景参考，**新代码请遵循 v3 约束**。

### matcher 岗位类别硬过滤（重要）

`matcher.md` prompt 必须先做**岗位类别一票否决**：
- ✅ 通过：title 含 `产品经理/PM/Product/产品总监/产品负责人` 等
- ❌ 否决：title 含 `助理/顾问/销售/经纪人/精算师/开发工程师/架构师/算法工程师` 等
  → 一律 `worth_applying=false, match_score<=0.3`，无论业务领域多匹配

**理由**：候选人 14 年产品经理经验，投销售/助理/技术岗是资历浪费且面试无意义。
即使业务领域强命中（如"保险服务顾问"涉及保险），只要不是产品经理类，就 worth=False。

### 删除 `score>=0.7 强制 worth=True` 策略

`matcher.py:154-160` 旧规则"score>=0.7 强制 worth=True"已删除。
新规则：`worth = worth_llm and hard_filter_passed and score >= 0.5`（三者同时满足）。
**理由**：旧规则让"业务助理-保险（固收）"这种 LLM 本来想拒的岗（reason 明写"按公式匹配但级别不匹配"）被强行 worth=True，绕过了 hard_filter_passed=False 的否决信号。代码层规则**不应该废掉 LLM 的判断权**。

### pipeline 入库前 title 白名单+黑名单过滤（源头堵漏）

BOSS 搜索"XX 产品经理"关键词时会混合召回大量非产品岗（业务助理/保险顾问/销售/精算师/技术支持）。
`pipeline.py:_is_title_acceptable` 在入库前按 title 过滤：

- 黑名单优先：title 含 `助理/实习/管培生/销售/经纪人/顾问/精算/核保/风控/开发工程师/架构师` 等 → 拒绝
- 白名单：title 含 `产品经理/PM/Product/产品总监/产品负责人/产品运营/产品岗/产品` 等 → 通过
- 既不含白名单也不含黑名单 → 拒绝（默认非产品岗）

**理由**：从源头堵住，避免 matcher/tailor/PDF/PNG 的 LLM+IO 成本浪费。
测试场景可设 `cfg.pipeline.bypass_title_filter=True` 跳过（fixtures 是 SEO 时代数据）。

### v3.1 web_greeter + nodriver_sender 浮层场景修复（2026-07-19 实测 chat_page_not_rendered 后）

**v3 教训**（已修正）：v3 曾尝试「nodriver 不点按钮，直接 page.get('/web/geek/chat')」，
但**裸 URL 不带 `?id=xxx&securityId=xxx` 参数**，Boss 聊天页拿不到沟通对象信息
→ SPA 部分渲染（只渲染联系人列表，聊天输入框不出现）→ `chat_page_not_rendered`
（实测 4ea11ee55ebd81d00nF-29i4EltV 稳定复现）。

**v2 成功路径**（4 个 image_sent 岗位验证过）：nodriver 在详情页点「继续沟通」按钮，
**Boss 会自动跳转到带参数的 `/web/geek/chat?id=xxx&securityId=xxx`**，SPA 完整渲染。

**v3.1 正确修复方案**（回归 v2 成功路径 + 保留 v3 删信号的简化）：

`web_greeter._detect_greet_outcome`（v3.1）：
1. 点头次「立即沟通」按钮
2. **只保留 3 种风控信号检测**（captcha / rate_limited / login_lost）
3. **删除其他 6 种成功信号**（v2 写的跳转/弹窗/toast/浮层，实测全不触发）
4. **等 8 秒**（原 v3 是 3s，太短 —— Boss 后端建立关系 + 按钮文案变「继续沟通」需 5-8s）
5. 无风控 → `relation_established=True`
6. sender quit DrissionPage → nodriver 接管

`nodriver_sender._navigate_to_chat`（v3.1）：
1. nodriver 在详情页找「继续沟通」按钮（兜底「立即沟通」），点击
2. **Boss 自动跳转** `/web/geek/chat?id=xxx&securityId=xxx`（带参数，SPA 完整渲染）
3. 等 3s + 5s + 轮询 40s 等 `.chat-input[contenteditable="true"]` 出现

**关键约束**：
- 裸 `/web/geek/chat`（无参数）渲染不出聊天输入框（实测）
- 必须通过点「继续沟通」让 Boss 自动生成带参数 URL
- 「继续沟通」按钮存在的前提：DrissionPage web_greeter 已点头次 + 等 8s

**已删除**：`web_greeter._has_greet_modal`（v2 写的 modal selector 是猜的，实测无效）。

## v3 通用化 matcher 重构（2026-07-19，ADR-0005）

### 核心变化

**v2 第三轮的"matcher 岗位类别硬过滤"和"pipeline title 白名单+黑名单"已被 v3 取代**。
原因：硬编码"产品经理"无法适配其他候选人（Java/销售/数据分析师等）。
v3 改为引入 `master.basics.candidate_profile`（候选人画像），从简历一次推导，
下游 matcher/pipeline/greet 据此动态判断。

### candidate_profile 字段（master.basics.candidate_profile）

```json
{
  "target_role_keywords": ["产品经理", "PM", "Product Manager"],
  "role_category": "产品经理",
  "seniority_level": "资深",
  "work_years_authoritative": 14,
  "core_industries": ["保险", "金融科技"],
  "exclusion_keywords": ["助理", "实习", "销售"]
}
```

### F1.55 候选人画像补全

- **位置**：体检（F1.6）之后、profiler（F1.5）之前
- **触发**：`master.basics.candidate_profile` 缺失或不完整时
- **实现**：`parser.derive_candidate_profile()` 调 LLM（复用 sonnet）从
  headline/projects/skill_modules 推导 6 个字段，写回 master.json 持久化
- **工龄强制对齐**：补全时强制 `work_years_authoritative == basics.work_years_total`
  （防 LLM 篡改工龄，ADR-0002）

### 体检闸门扩展

`hrbp_check.validate_fixes` 新增 `_validate_candidate_profile`：
- target_role_keywords 非空（3-8 个）
- role_category 非空
- seniority_level ∈ {初级,中级,高级,资深,专家}
- work_years_authoritative == work_years_total
- core_industries 非空

违反 → 体检失败（闸门阻断 F1.5/F4）。

### matcher v3 完全去硬编码

`matcher.md` 完全重写：
- **岗位类别判断**：基于 candidate_profile.target_role_keywords + exclusion_keywords
- **资历级别判断**（新维度）：输出 `level_match`
  （overqualified/match/underqualified/unknown）
- **公式调整**：`match_score = 0.75*hard_skill + 0.25*business_domain`
  （资深 PM 方法论迁移是核心，业务领域 6-12 个月可补齐）
- **hard_filter 对齐 score**：`hard_filter_passed = business_domain_match_score >= 0.5`
  （消除 score 与 hard_filter 口径不一致）
- **业务领域基于 JD 判断**：business_domain_match_score 必须**基于 JD 实际涉及的业务**，
  不是候选人背景（修 C 端 PM 业务领域被误打 1.0 的 bug）

`matcher.analyze_jd` worth 四要素（同时满足）：
```python
worth = worth_llm and category_passed and level_pass and hard_filter_passed and score >= 0.5
# level_pass = level_match in ("match", "unknown")
```

### DB v3 迁移

`jobs` 表新增 4 列（不重建表，用 `_try_add_column` 兜底加列）：
- `level_match TEXT`
- `job_category TEXT`
- `category_passed INTEGER`（bool→int 转换）
- `business_domain_match_score REAL DEFAULT 0`

老数据这些列为 NULL/0，下次 matcher 重跑时回填。
`transition()` 白名单、`upsert_job()` INSERT/UPDATE 都已支持新列。

### 删除内容

- `pipeline.py:_TITLE_WHITELIST` / `_TITLE_BLACKLIST` / `_is_title_acceptable`（全部删除）
- `config.py:bypass_title_filter`（删除；老 config.yaml 用 `extra="allow"` 兼容忽略）
- `tests/unit/test_pipeline_title_filter.py`（删除 22 个 case，逻辑已迁移到 matcher）

### F1 prompt 外置

F1 prompt 从 `parser.py` 内联抽到 `prompts/parser.md`（与其他 stage 对齐），
新增「候选人画像推导方法论」章节。`render_parser_prompt()` 镜像
`render_profiler_prompt()` 模式。

### 相关测试

- `tests/unit/test_candidate_profile.py`（24 case）：画像 Pydantic + 补全 + 体检
- `tests/unit/test_matcher.py`（18 case）：matcher v3 全套
- `tests/unit/test_db_migration_v3.py`（7 case）：DB v3 迁移
- `tests/fixtures/master.sample.json`：新增 candidate_profile 字段样例
