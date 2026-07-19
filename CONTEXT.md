# Boss 直聘智能投递助手

为求职者自动完成「理解简历→交互审查→HRBP体检→搜索岗位→针对性定制→生成PDF→自动投递」全闭环的工具。最终目标是**找到工作**，不是「投出去」。

> **开源说明**：本文档描述项目的领域语言和数据标准。所有候选人专属信息（姓名/手机/方向/年限）从 `config.yaml` 和 `master.json` 动态读取，不在代码或文档中硬编码。

## Language

**求职者（JobSeeker）**:
简历主人，即本工具的使用者。候选人专属信息（姓名/手机/邮箱/方向/年限/学历）从 `config.yaml` 的 `target_constraints` 和 `master.json` 的 `basics`/`constraints` 动态读取。
_别名_: 用户、候选人

**履历母库（master.json）**:
从原始简历解析出的、包含求职者所有真实经历/技能/项目的结构化数据。定制简历时只能从中选材重组，**不可凭空生成**。

**定制简历（Tailored Resume）**:
针对单个岗位 JD，从履历母库选材+重排+润色生成的 .typ 源文件，编译成 PDF/PNG 投递。每岗一份。

**求职方向（Target）**:
由 F1.5 简历画像分析推荐、用户确认的一组搜索参数（方向名/城市/关键词/硬技能/薪资）。

**候选人画像（candidate_profile，ADR-0005）**:
`master.basics.candidate_profile`，从简历一次推导的通用化基座。
含 6 个字段：target_role_keywords（目标岗位关键词）、role_category（核心职业类别）、
seniority_level（资历级别 5 档）、work_years_authoritative（可验证工龄）、
core_industries（核心行业）、exclusion_keywords（排除词）。
下游 matcher/pipeline/greet 据此动态判断，取代"产品经理白名单"等硬编码。
F1 解析时由 LLM 推导，老 master.json 由 F1.55 补全，体检闸门校验字段完整性。

**投递（Application）**:
一次完整的「打招呼（nodriver 文字话术）+ 发图片简历（nodriver 图片上传）」动作，针对单个岗位。
_技术细节_: 聊天页发送用 nodriver 绕过 zpAegis 反爬（ADR-0003）；登录+搜索用 DrissionPage。

**简历存疑点审查（F1.7）**:
简历解析后，LLM 审查 master.json 识别存疑点（经历断层/数据缺失/技能模糊/信息不一致），CLI 逐条提问并附建议方向。用户回答写入 master.json 的 `qa_log` 字段。

**润色红线（Polish Line）**:
定制简历时允许与禁止的改写边界。当前为「允许适度润色」（强化动词/量化/术语对齐），禁止伪造经历/学历/公司/年限。
**核心规则**：任何量化数据必须有真实依据，不可「凭行业经验编造不夸张的数值」——这属于伪造。无数据则写定性描述。

**数据表述标准（ADR-0002）**:
量化数据必须**可信 + 可验证 + 有基数参照**三者同时满足，否则降级为定性描述。
- 用「绝对值+结果」替代「百分比」（无基数时）
- 用「过程价值」替代「小体量结果」
- 用「数量+体系」替代「反向金额」

**学历筛选（Degree Filter）**:
config 的 `target_constraints.degree` 字段设为求职者的真实学历，投递学历要求匹配的岗位。
_别名_: 学历门槛

**薪资带（Salary Band）**:
按城市分档，从 `config.yaml` 的 `target_constraints.salary` 读取（如 `{"上海": "15-25K"}`）。
_别名_: 期望薪资、target_salary

**核心求职方向（Core Direction）**:
从 `config.yaml` 的 `target_constraints.primary_direction` 读取（如「后端开发」「产品经理」「设计师」）。
**F1.5 约束**（v2 调整，2026-07-19）：`primary_direction` 仅作为 LLM 推导的**提示**，
LLM 完全可以从 master 推导出池外的方向（如 master 中保险项目多的用户，即使 config 没声明保险，
LLM 也会推荐"保险科技产品经理"方向）。`profiler.validate_targets` 不再强制 sub_direction ∈ config 池。
_别名_: 主方向、primary_direction

**子方向（Sub-Directions）**:
F1.5 推荐的子方向。v2 后**由 LLM 自主推荐**（基于 master 的 work_experiences + projects + skill_modules），
`config.yaml` 的 `target_constraints.sub_directions` 降级为 keywords_hint（可选池辅助 LLM，不强制）。
约束：weight 总和 ≈ 1.0、(sub_direction, city) 不重复、city 合法、sub_direction 非空。

**工作年限口径**:
从 `master.json` 的 `basics.work_years_total` 读取（简历解析时提取）。任何 LLM 润色不得把年限往大改。
`constraints.work_years_authoritative` 为可验证年限，用于后置校验。
_别名_: 真实年限、可验证年限

**禁忌词（Forbidden Words）**:
分两层：
- **通用过度承诺词**（代码内置，`tailor.FORBIDDEN_PATTERNS`）：精通、100%、完美、极致
  - **政策**（2026-07-16 调整）：禁忌词**只告警不阻断**——LLM 输出含禁忌词时 `validate_tailored` 记 warning 但不重试/不抛错。阻断级违规仅限「幻觉项目」「工作年限超限」。
  - 候选人可用「端到端」「熟练掌握」等更克制的表达替代
- **候选人专属词**（config 注入）：`target_constraints.forbidden_words_extra`，如 `["300%", "7年"]`

**浏览器双引擎架构（ADR-0003）**:
- **DrissionPage**：登录 + 搜索 + 详情页操作（zpAegis 不拦截这些页面）
- **nodriver**：聊天页发送话术 + 图片（zpAegis 检测 CDP Runtime.enable 阻断 zpToken，
  nodriver 不调该命令从而绕过）
- ⚠️ 两者不能同时用同一 profile（会互相覆写 cookies）

## Relationships

- 一个**求职者**有且仅有一份**履历母库**
- 一个**求职方向**包含一个城市 + 一组关键词，生成多份**定制简历**
- 一份**定制简历**对应一次**投递**
- 所有**投递**必须在**润色红线**之内

## 多格式简历解析（ADR-0004）

简历输入支持 4 种格式（`config.yaml` 的 `paths.resume_input` 指定文件路径）：

| 格式 | 解析器 | 降级链 |
|------|--------|--------|
| `.pdf` | DoclingParser → PymupdfRuleParser | docling 装不上时降级 |
| `.docx` | DoclingParser → PythonDocxParser | docling 装不上时降级 |
| `.md` | TextRuleParser | 无降级（纯 Python） |
| `.txt` | TextRuleParser | 无降级（纯 Python） |
