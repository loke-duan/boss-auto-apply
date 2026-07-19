# 变更记录 / Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 格式，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 计划中
- 多平台支持（拉勾/智联/猎聘）
- 英文 README + 英文 prompt
- Web UI（替代 CLI 交互）

---

## [1.0.0] - 2026-07-19

🎉 **首个公开版本**。完整闭环：理解简历 → HRBP 体检 → 候选人画像 → 方向推荐 → 搜索岗位 → JD 匹配 → 简历定制 → PDF/PNG 生成 → 自动投递（招呼+发图片）。

### ✨ 核心特性

#### 简历处理
- **多格式简历解析**（ADR-0004）：支持 PDF / DOCX / Markdown / TXT，docling 主 + PyMuPDF/python-docx 兜底双引擎
- **交互式简历审查**（F1.7）：识别存疑点（经历断层、数据缺失、技能模糊），CLI 逐条提问
- **HRBP 体检闸门**（ADR-0001）：投递前由 LLM 模拟 HRBP 角色体检，用户确认后才解锁
- **数据诚信三规则**（ADR-0002）：量化数据必须可信+可验证+有基数，禁止凭空编造

#### 通用化匹配
- **候选人画像 candidate_profile**（ADR-0005）：从简历一次推导（目标岗位/资历级别/核心行业/排除词），下游 matcher/pipeline 通用复用
- **不硬编码任何角色**：产品经理/Java/销售/数据分析师都能用同一套代码（取代旧的"产品经理白名单"硬编码）
- **三维 matcher 评分**：
  - 岗位类别判断（category_passed）：基于 candidate_profile 一票否决
  - 资历级别判断（level_match）：防 overqualified（如资深 PM 投助理岗）
  - 公式：`match_score = 0.75*hard_skill + 0.25*business_domain`
- **hard_filter 对齐 score**：`hard_filter_passed = business_domain_match_score >= 0.5`（消除"score 高却 hard_filter=False"的矛盾）

#### 自动投递
- **双引擎浏览器架构**（ADR-0003）：
  - DrissionPage：登录+搜索+详情页+点头次「立即沟通」
  - nodriver：聊天页发文字+图片（绕过 zpAegis 反爬）
- **流式架构**：采一个投一个，搜到岗位立即走完全链路再搜下一个
- **断点续传**：任何步骤失败都留原状态，`resume` 自动重试
- **限流+熔断**：6 道闸门（日上限/间隔/连投/活跃时段/预热/熔断）+ 3 态熔断器（连续失败/失败率/硬熔断）
- **个性化招呼**（F7）：根据 JD + master 强相关项目生成 2-3 句招呼话术

### 🏗️ 架构
- **14 个核心模块**：parser / qa / hrbp_check / profiler / matcher / tailor / generator / imager / sender + 6 个浏览器模块
- **状态机驱动**：found → jd_analyzed → resume_tailored → pdf_generated → image_ready → greeted → text_sent → image_sent
- **6 个 prompt 模板**：parser / hrbp_check / profiler / matcher / tailor / greet
- **SQLite v3 持久化**：v1 基础 + v2 添加 text_sent + v3 添加 matcher 通用化字段

### 🧪 测试
- **407+ test cases** 全绿
- 覆盖：单元 + 集成 + prompt 回归 + DB 迁移 + 候选人画像 + typst 大小写归一化
- 覆盖率 74%

### 📐 架构决策（ADR）
- [ADR-0001](docs/adr/0001-hrbp-health-check-mandatory.md)：HRBP 体检强制
- [ADR-0002](docs/adr/0002-data-integrity-over-numbers.md)：数据诚信三规则
- [ADR-0003](docs/adr/0003-nodriver-for-chat-page.md)：nodriver 用于聊天页（绕过 zpAegis）
- [ADR-0004](docs/adr/0004-multi-format-resume.md)：多格式简历解析
- [ADR-0005](docs/adr/0005-candidate-profile-generalization.md)：候选人画像通用化基座

### ⚠️ 已知限制
- 仅支持 Boss直聘（暂不支持其他招聘平台）
- 仅中文界面和 prompt
- 真 Boss 行为可能随平台改版失效（需持续维护 selectors）
- 风险：违反 Boss 用户协议，存在账号风控/封禁风险（详见 [DISCLAIMER.md](DISCLAIMER.md)）
