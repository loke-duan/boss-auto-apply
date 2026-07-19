# SKILL.md — boss-auto-apply 薄编排入口

> 本文件是 zcode agent 的「编排入口」（调试期交互式走单岗流程）。
> 无人值守主控由 Python 包 `boss_auto_apply` 负责（`python -m boss_auto_apply`）。
> 设计要求 < 300 行。
> ⚠️ AI Agent 工作前必读 `AGENTS.md`（项目约束总览）。

## 何时触发

用户说以下任一时触发：
- 「帮我投简历」「自动投 BOSS」「boss 自动投递」
- 「跑一下 boss-auto-apply」「dry-run 简历」
- 「看一下投递状态」「简历体检」

## 能力边界

✅ **能做**（M1+M2+M3 已验证）：
- 简历解析（PDF/DOCX/MD/TXT → master.json，多格式 dual-parser + LLM，ADR-0004）
- 简历存疑点审查 + 交互问答（F1.7，CLI 逐条提问并附建议）
- HRBP 体检（暂停等确认，ADR-0001）
- 方向推荐（暂停等确认）
- 岗位搜索（DrissionPage 真搜索 Boss 网页）
- JD 匹配 + 简历定制（Claude CLI，score≥0.7 强制 worth=True）
- PDF/PNG 生成（Typst + brilliant-CV v4）
- dry-run 全链路
- **真发送打招呼 + 图片简历**（nodriver 绕过 zpAegis，ADR-0003，已实测送达）

❌ **不做**（M4+）：
- 限流真跑 daily_quota 扣减（RealRateLimiter 已实现但未真跑）
- SkillSpector 安全门禁（M5）

## 标准流程

### 1. 预检

```bash
python -m boss_auto_apply preflight
```

确认 node / claude CLI / typst / 字体 / 模板就位。dry-run 可跳过登录。

### 2. 登录（真发送前必须）

```bash
python -m boss_auto_apply login
```

用 DrissionPage 打开 Boss 扫码页，扫码后 cookies 存入 `~/.boss-auto-apply/chrome-profile`。

### 3. dry-run（验证全链路，不真发）

```bash
python -m boss_auto_apply --non-interactive dry-run
```

### 4. 真发送（nodriver 引擎）

```bash
# 确保 config.yaml: sender.driver = "nodriver"
# 确保无残留 Chrome 进程
pkill -9 -f "Google Chrome"

python -m boss_auto_apply run
```

### 5. 看状态 / 断点续传

```bash
python -m boss_auto_apply status
python -m boss_auto_apply --non-interactive resume
```

## 交互式调试（单岗观察）

1. `python -m boss_auto_apply parse` → 看 master.json
2. `python -m boss_auto_apply hrbp-check` → 看体检报告，手动改 master.json
3. `python -m boss_auto_apply profile` → 看推荐方向，手动改 targets.confirmed.yaml
4. （循环）对单个 job 观察 matcher/tailor 输出

## 关键约束（红线）

⚠️ 详见 `AGENTS.md` 的完整约束总览。核心：

- **体检是闸门**（ADR-0001）：master.health_check_status != 'passed' 时，画像/定制拒绝运行。
- **数据诚信**（ADR-0002）：所有数值可信+可验证+有基数参照，否则降级为定性描述。
- **工作年限**：从 master.json basics.work_years_total 读取（简历解析时提取），LLM 不得改大。
- **禁忌词**：通用词（精通/全链路/100%/完美/极致）代码内置；候选人专属词从 config.forbidden_words_extra 注入。
- **禁伪造**：项目/公司/学历/年限/数据。
- **简历项目保留全部**（时间倒序），匹配的重点改写，不匹配的简化保留。
- **候选人信息**：姓名/手机/邮箱/方向/薪资均从 config.yaml 和 master.json 动态读取，不在代码硬编码。

## 浏览器引擎（ADR-0003）

| 操作 | 引擎 |
|---|---|
| 登录 + 搜索 + 详情页 | DrissionPage |
| 聊天页发送话术+图片 | nodriver（绕过 zpAegis） |

⚠️ 用 nodriver 前必须 `pkill -9 -f "Google Chrome"`（profile 互斥）。

## 配置

见 `config/config.yaml`（从 `config/config.example.yaml` 拷贝）。关键：
- `sender.driver: "nodriver"`（真发送）/ `"drissionpage"`（仅 dry-run）
- `search.provider: "drissionpage"`（真搜索）/ `"mock"`（测试）
- `llm.model_tailor/profiler/hrbp: claude-sonnet`（强），`matcher/greeter: claude-haiku`（便宜）

## 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `ClaudeLoginRequiredError` | CLI 未登录 | `claude auth login` |
| `TypstNotInstalledError` | typst 未装 | `brew install typst` |
| docling 装不上 | Python 3.13 | 用 PyMuPDF 兜底（已实现） |
| 聊天页 SPA 不渲染 | DrissionPage 被 zpAegis 检测 | 切 `sender.driver: "nodriver"`（ADR-0003） |
| nodriver profile 冲突 | DrissionPage Chrome 未关 | `pkill -9 -f "Google Chrome"` 后再用 nodriver |
| `boss` command not found | boss-cli 未装 | 已弃用（stoken 缺陷），用 DrissionPage |

详细见 `docs/HANDOVER.md` §5 和 `docs/adr/0003-nodriver-for-chat-page.md`。
