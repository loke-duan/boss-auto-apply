# ADR-0003：nodriver 用于聊天页发送（绕过 zpAegis 反爬）

## 状态

已采纳（2026-07-12 实测验证，话术+图片已成功送达 MiniMax）

## 背景

M3 设计（`docs/M3_DESIGN.md`）原定「全 DrissionPage 单通道」架构。但实测发现
Boss 聊天页（`/web/geek/chat`）的 Vue SPA 在 DrissionPage 自动化浏览器中**不渲染**：

- HTML 始终 4644 字符（空白骨架）
- `#wrap` 容器 `innerHTML` 仅 21 字符（3 个空 Vue 注释占位符 `<!---->`）
- 0 个 textarea / contenteditable / file input
- `document.body.innerText` 为空

### 根因分析

Boss 聊天页加载 `zpAegis` 安全 SDK（`https://www.zhipin.com/zhipin-security/web/geek/index.js`，
92KB 混淆）。zpAegis 检测 CDP（Chrome DevTools Protocol）的 `Runtime.enable` 调用——
这是 Puppeteer / Playwright / DrissionPage / Scrapling 自动化时都会发出的命令，
会在 V8 运行时留下可检测的插桩痕迹。

检测到自动化后，zpAegis **阻断 `/wapi/zppassport/set/zpToken` API**：
- DrissionPage 下：请求 2ms 即返回，0 字节，无 HTTP 状态（被 zpAegis 客户端拦截）
- 没有 zpToken → 用户信息 wapi 不加载 → `hasLoadedUser=false`
- Vue Router 的 `beforeEnter` 守卫拒绝导航到 `/web/geek/chat` → 路由卡在 `/`
- 根组件渲染空（3 个 Vue 注释占位符）

### 已排除的方案

| 方案 | 结果 | 原因 |
|---|---|---|
| DrissionPage + stealth.js | ❌ 失败 | stealth.js 只改 `navigator.webdriver`，不阻止 `Runtime.enable` 调用 |
| Scrapling StealthyFetcher | ❌ 失败 | Playwright 同样调 `Runtime.enable`；且与 DrissionPage profile 冲突 |
| 半人工模式 | ⚠️ 备选 | 可行但非自动化，不符合项目目标 |

## 决策

**聊天页发送用 nodriver，登录+搜索仍用 DrissionPage**（双引擎架构）。

### nodriver 绕过原理

nodriver（undetected-chromedriver 的继任者，`pip install nodriver`）用原始 CDP
通信但**刻意不调 `Runtime.enable`**，从而不留下 zpAegis 检测的痕迹。

### 实测对比

| 检查项 | DrissionPage（失败） | nodriver（成功） |
|---|---|---|
| zpToken 请求 | 2ms, 0 字节, 无状态 | 37ms, 361 字节, **HTTP 200** |
| `hasLoadedUser` | false | true |
| `isChatPage` | undefined | **true** |
| `#wrap` innerHTML 长度 | 21（空注释） | **34616**（完整 SPA） |
| textarea/contenteditable | 0 / 0 | 0 / **1**（`.chat-input`） |
| file input | 0 | **3** |

### 架构

```
登录 + 搜索 + 详情页    →  DrissionPage（BrowserManager + WebSearcher + WebGreeter）
聊天页发送话术+图片     →  nodriver（NodriverChatSender，asyncio.run 桥接）
```

- `WebChatSender`（`boss_auto_apply/browser/web_chat_sender.py`）按 `config.sender.driver`
  分发：`"nodriver"` → 委托 `NodriverChatSender`；`"drissionpage"` → 现有逻辑（仅 dry-run 有意义）
- `NodriverChatSender`（`boss_auto_apply/browser/nodriver_sender.py`）：同步 API，
  内部用 `asyncio.run` 调 async nodriver；自管浏览器生命周期

## 后果

### 正面
- ✅ 聊天页 SPA 正常渲染，话术+图片可自动发送
- ✅ 登录+搜索仍用 DrissionPage（不需改现有搜索/登录流程）
- ✅ `SenderCfg.driver` 字段已存在，配置切换零成本

### 负面 / 约束
- ⚠️ **profile 互斥**：nodriver 和 DrissionPage 不能同时用同一 `user_data_dir`——
  会互相覆写 cookies。用 nodriver 前必须 `pkill -9 -f "Google Chrome"`
- ⚠️ **async→sync 桥接**：nodriver 是 async API，项目是纯 sync。`NodriverChatSender`
  用 `asyncio.run` 桥接，每次 `send_full` 启动并关闭浏览器（不复用实例）
- ⚠️ **Scrapling 弃用**：Scrapling 与 DrissionPage profile 冲突，且 Playwright 的
  `Runtime.enable` 会被 zpAegis 检测，已从架构中移除
- ⚠️ 聊天输入框是 `contenteditable`（不是 textarea），`selectors.py` 已更新

## 参考

- nodriver GitHub: https://github.com/ultrafunkamsterdam/nodriver
- rebrowser: CDP Runtime.enable 检测原理: https://rebrowser.net/blog/how-to-fix-runtime-enable-cdp-detection
- 交接文档: `docs/HANDOVER.md` §5
