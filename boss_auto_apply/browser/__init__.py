"""浏览器自动化子包（M3 双引擎：DrissionPage + nodriver）。

模块：
- ``selectors``：Boss 网页 DOM 选择器集中管理（设计 §12）。
- ``stealth.js``：反检测脚本（document_start 注入，设计 §8）。
- ``manager``：BrowserManager 浏览器生命周期 + profile + stealth（设计 §4，DrissionPage）。
- ``web_searcher``：WebSearcher 网页版搜索（设计 §5，DrissionPage）。
- ``web_greeter``：WebGreeter 点击沟通（设计 §6，DrissionPage）。
- ``web_chat_sender``：WebChatSender 发话术 + 发图片（设计 §7，双引擎分发）。
- ``nodriver_sender``：NodriverChatSender 聊天页发送（ADR-0003，绕过 zpAegis 反爬）。

双引擎架构（ADR-0003）：
- **DrissionPage**：登录 + 搜索 + 详情页操作（zpAegis 不拦截这些页面）。
- **nodriver**：聊天页发送话术 + 图片（zpAegis 检测 CDP Runtime.enable 阻断 zpToken，
  nodriver 不调 Runtime.enable 从而绕过）。
- ⚠️ 两者**不能同时用同一 profile**（会互相覆写 cookies）。
"""

from __future__ import annotations

__all__: list[str] = []
