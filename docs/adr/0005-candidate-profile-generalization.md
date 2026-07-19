# ADR-0005: 候选人画像（candidate_profile）通用化基座

**日期**：2026-07-19
**状态**：已采纳

## 背景

项目核心目标是**通用化、可适配、可迁移**——任何候选人（产品经理/Java 开发/销售总监/...）
都能用自己的简历驱动整套投递流程。

但 v2 第三轮迭代里，为了让 matcher 拒绝非产品经理岗位，引入了大量产品经理硬编码：

1. **`pipeline.py` 的 `_TITLE_WHITELIST` / `_TITLE_BLACKLIST`**：
   - 白名单写死"产品经理/PM/Product/产品总监/..."
   - 黑名单写死"助理/实习/销售/经纪人/精算师/..."
2. **`prompts/matcher.md` 第 1 节"岗位类别硬过滤"**：
   - 写死"候选人定位是产品经理类岗位"
   - 写死"非产品经理类（销售/助理/技术/精算师等）→ 一票否决"
3. **`prompts/profiler.md` 示例**：
   - 写死"保险科技产品经理-上海"作为推荐方向示例
4. **`prompts/greet.md` 示例**：
   - 写死"看到贵司保险产品经理岗位"作为招呼话术示例

### 硬编码带来的问题

- **无法适配其他候选人**：若候选人是 Java 开发，所有白名单/黑名单/prompt 约束全部失效
- **维护成本**：每个新方向（如数据分析师）都要改代码 + 改 prompt
- **测试脆弱**：22 个 title 过滤测试与具体关键词绑定，重构代价高

### 同时发现的 matcher 设计缺陷

- `match_score = 0.6*hard_skill + 0.4*business_domain`：业务领域权重过高，
  导致资深 PM 投跨行业岗位（如文旅产品经理）评分偏低
- `hard_filter` 用 `per_job_keywords AND`：每个关键词都必须命中，
  导致"score=0.85 却 hard_filter=False"的自相矛盾（C 端产品经理 score=0.85 被 skip）
- 缺少 `level_match` 维度：14 年资深 PM 投"业务助理"初级岗
  在公式上必然 score>0.7，但实际不该投

## 决策

引入 **`master.basics.candidate_profile`**（候选人画像）作为通用化基座，
取代所有硬编码。画像字段从简历一次推导，下游多处复用。

### 1. master.json 新增 candidate_profile 字段

```json
{
  "basics": {
    "candidate_profile": {
      "target_role_keywords": ["产品经理", "PM", "Product Manager"],
      "role_category": "产品经理",
      "seniority_level": "资深",
      "work_years_authoritative": 14,
      "core_industries": ["保险", "金融科技"],
      "exclusion_keywords": ["助理", "实习", "销售"]
    }
  }
}
```

字段语义：
- `target_role_keywords`：BOSS 搜索用的目标岗位关键词 3-6 个
- `role_category`：单一核心职业类别（如 "产品经理"/"Java 开发"/"销售总监"）
- `seniority_level`：5 档枚举（初级/中级/高级/资深/专家）
- `work_years_authoritative`：可验证工龄（必须 == `basics.work_years_total`）
- `core_industries`：核心行业经验 1-4 个
- `exclusion_keywords`：明确排除的方向词

### 2. F1.55 候选人画像补全

新增 `parser.derive_candidate_profile()`：当老 master.json 缺失 candidate_profile 时，
由 LLM 基于简历素材（headline/projects/skill_modules）补全。

流程位置：体检（F1.6）之后、profiler（F1.5）之前。已通过体检的 master 自动跑补全，
写回 master.json 持久化。

### 3. 体检闸门扩展

`hrbp_check.validate_fixes` 新增 candidate_profile 校验：
- target_role_keywords 非空（3-8 个）
- role_category 非空
- seniority_level ∈ 5 档枚举
- work_years_authoritative == basics.work_years_total（防 LLM 篡改工龄，ADR-0002）
- core_industries 非空

违反 → 体检失败（闸门阻断 F1.5/F4）。

### 4. matcher 完全去硬编码

`prompts/matcher.md` 重写：
- **岗位类别判断**：基于 `candidate_profile.target_role_keywords` + `exclusion_keywords`
- **资历级别判断**：基于 `candidate_profile.seniority_level` + `work_years_authoritative`，
  输出 `level_match`（overqualified/match/underqualified/unknown）
- **公式调整**：`match_score = 0.75*hard_skill + 0.25*business_domain`
  （资深 PM 方法论迁移是核心，业务领域可补齐）
- **hard_filter 对齐 score**：`hard_filter_passed = business_domain_match_score >= 0.5`
  （消除 score 与 hard_filter 口径不一致）
- **业务领域基于 JD 判断**：`business_domain_match_score` 必须基于 JD 实际涉及的业务，
  不是候选人背景（修 C 端 PM 业务领域被打 1.0 的误判）

`matcher.analyze_jd` 新增 worth 四要素：
```
worth = worth_llm AND category_passed AND level_pass AND hard_filter_passed AND score >= 0.5
```

### 5. 删除 pipeline 层 title 过滤

`pipeline.py` 删除 `_TITLE_WHITELIST` / `_TITLE_BLACKLIST` / `_is_title_acceptable`。
所有 title 都入库，由 matcher 基于 candidate_profile 判断 category_passed + level_match。

`config.py` 删除 `bypass_title_filter` 字段（老 config.yaml 用 `extra="allow"` 兼容忽略）。

### 6. DB v3 迁移

`jobs` 表新增 4 列（不重建表，用 `_try_add_column` 兜底）：
- `level_match TEXT`
- `job_category TEXT`
- `category_passed INTEGER`
- `business_domain_match_score REAL DEFAULT 0`

老数据这些列为 NULL/0，下次 matcher 重跑时回填。

## 后果

### 正面

- **完全通用化**：不同候选人（PM/Java/销售）画像不同，matcher 据此动态判断，
  无需改代码或 prompt
- **去自相矛盾**：score 与 hard_filter 口径一致（business_domain_match_score），
  不会再出现"score=0.85 却 worth=False"
- **防 overqualified**：14 年 PM 投业务助理初级岗会被 level_match 一票否决
- **业务领域误判修复**：C 端 PM 业务领域基于 JD 判断，不会误打 1.0
- **可验证**：candidate_profile 走体检闸门，工龄篡改会被发现

### 负面 / 取舍

- **F1 多一次 LLM 调用**：candidate_profile 推导复用 profiler 模型（sonnet），
  一次约 2-3 分钟。但有缓存（写回 master.json 后不再重跑）
- **测试重写成本**：删除 22 个 title 过滤测试，新增 18 个 matcher v3 测试 +
  6 个 candidate_profile 测试 + 6 个 DB migration 测试

### 风险与缓解

- **F1.55 推导失败**：若 LLM 输出非 dict，`derive_candidate_profile` 静默返回原 master。
  下次体检会因 candidate_profile 缺失而 fail，提示用户手工补全。
- **LLM 漂移**：level_match 判定依赖 LLM，可能漂移。代码层归一化非法值为 "unknown"，
  且 overqualified/underqualified 会硬阻断 worth（不会让坏判断通过）。

## 相关文档

- `prompts/parser.md`：F1 解析 prompt（含 candidate_profile 推导方法论）
- `prompts/matcher.md`：F3 匹配 prompt（含 category/level_match/business_domain 三维判断）
- `AGENTS.md` §v3：通用化 matcher 重构约束
- `tests/unit/test_candidate_profile.py`：画像 Pydantic + 补全 + 体检测试
- `tests/unit/test_matcher.py`：matcher v3 全套测试
- `tests/unit/test_db_migration_v3.py`：DB v3 迁移测试
