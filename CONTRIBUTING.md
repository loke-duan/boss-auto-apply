# 贡献指南 / Contributing Guide

感谢你对 boss-auto-apply 的兴趣！欢迎提交 Issue、PR 或建议。

## 📋 开发前必读

在贡献代码前，请先阅读以下文档（**这些是项目的「事实唯一源」**）：

| 文档 | 用途 |
|------|------|
| [README.md](README.md) | 项目介绍 + 快速开始 |
| [AGENTS.md](AGENTS.md) | **AI agent / 开发者约束总览**（红线规则 + 各阶段约束） |
| [CONTEXT.md](CONTEXT.md) | 领域语言 + 数据表述标准 |
| [docs/adr/](docs/adr/) | 架构决策记录（5 份 ADR，每个重大决策的来龙去脉） |

**特别注意 AGENTS.md 的「核心约束（红线）」段**：
- ADR-0001：体检是闸门，未通过不得投递
- ADR-0002：数据诚信三规则（可信+可验证+有基数）
- ADR-0005：候选人画像通用化（不硬编码任何角色）
- 禁伪造：项目/公司/学历/年限/数据一律不可凭空生成

## 🛠️ 开发环境

```bash
# 克隆 + 安装
git clone https://github.com/yeatssc/boss-auto-apply.git
cd boss-auto-apply
bash setup.sh  # 一键安装 conda + Python 依赖 + typst + 字体 + 模板

# 激活环境
conda activate boss-auto

# 跑测试（应全绿）
python -m pytest tests/ -q
```

详见 [README.md](README.md) 的「快速开始」段。

## 🧪 测试要求

**所有 PR 必须保证测试全绿**：

```bash
python -m pytest tests/ -q --no-cov
# 期望：407+ passed, 0 failed
```

测试组织：
- `tests/unit/`：单元测试（每个 core/ 模块对应一个 test_*.py）
- `tests/integration/`：集成测试（流式主流程、状态机、断点续传）
- `tests/fixtures/`：mock 数据（master.sample.json / jd.sample.json / dom_samples.py）

**新功能必须配套测试**：
- 新增 core/ 模块 → 加 `tests/unit/test_<module>.py`
- 新增 prompt → 加 prompt 回归测试
- 新增 DB 字段 → 加 migration 测试（参考 `test_db_migration_v3.py`）

## 🔄 提交规范

### Commit Message 格式

```
<type>(<scope>): <subject>

<body>
```

**type**：
- `feat`：新功能（如 feat(matcher): 增加 level_match 维度）
- `fix`：修复（如 fix(nodriver): 修正聊天页 SPA 渲染失败）
- `docs`：文档（如 docs(readme): 补充 ADR-0005）
- `refactor`：重构（如 refactor(matcher): 删除硬编码白名单）
- `test`：测试（如 test(candidate_profile): 新增画像 Pydantic 校验）
- `chore`：杂项（如 chore(deps): 升级 nodriver 到 0.50）

**scope**：模块名（matcher / nodriver / pipeline / db / parser / hrbp / profiler / tailor / generator / config / docs）

### PR 流程

1. **Fork + Clone**：fork 到自己的 GitHub，clone 到本地
2. **建分支**：`git checkout -b feat/your-feature`（不要在 main 上直接开发）
3. **写代码 + 写测试**：保证 `pytest tests/ -q` 全绿
4. **跑预检**：`python -m boss_auto_apply preflight`（确认环境就绪）
5. **dry-run 验证**：`python -m boss_auto_apply --non-interactive dry-run`（验证全链路）
6. **提交 PR**：描述清楚做了什么、为什么、怎么测试的

## 🎯 优先欢迎的贡献方向

- **多平台支持**：目前只支持 Boss直聘，欢迎扩展到拉勾/智联/猎聘等
- **多角色画像**：测试不同 role_category（Java/销售/数据分析）的 candidate_profile 推导效果
- **简历模板扩展**：目前用 brilliant-CV，欢迎加其他 typst 模板
- **限流策略优化**：根据实际风控反馈调整 `limits` / `circuit_breaker` 参数
- **测试覆盖**：提升当前 74% 覆盖率（重点在 preflight/main/sender）
- **国际化**：英文 README / 英文 prompt（当前全中文）

## ⚠️ 不接受的贡献

- **删除或弱化红线约束**（体检闸门、数据诚信、禁伪造）
- **降低风控防护**（删除限流、熔断）
- **增加伪造能力**（编造项目、夸大工龄、生成虚假数据）
- **绕过特定平台**的反爬（与平台具体漏洞绑定的代码）

## 📮 联系方式

- **Bug 反馈**：[提交 Issue](https://github.com/yeatssc/boss-auto-apply/issues)
- **功能建议**：先开 Issue 讨论，达成共识后再开发
- **安全漏洞**：请勿公开 Issue，发邮件到 GitHub 账号关联邮箱

## 📄 License

贡献的代码将遵循 [MIT License](LICENSE)。
