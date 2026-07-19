// ============================================================
// stealth.js — Boss 反检测 hook（document_start 注入，设计 §8）
// 基于 get_jobs Discussion #250 三步法 + 0xsdeo/AntiDebug_Breaker
// 注入方式：BrowserManager._inject_stealth_js()
//   tab.run_cdp('Page.addScriptToEvaluateOnNewDocument', source=...)
//   + 对已打开 tab 立即 tab.run_js(stealth_js)
// ============================================================
(function () {
  'use strict';

  // ---- 1. 清除 navigator.webdriver 指纹 ----
  // 自动化浏览器会暴露 navigator.webdriver=true，Boss 据此识别
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: function () { return undefined; },
      configurable: true,
    });
  } catch (e) { /* ignore */ }

  // ---- 2. 伪造 Function.prototype.toString 完整性 ----
  // Boss 检测 hook 后的函数 toString 是否暴露原生代码
  var nativeToString = Function.prototype.toString;
  var fakeNativeToString = function () {
    if (this === fakeNativeToString) return nativeToString.call(this);
    // 已 hook 的函数返回伪造的「function xxx() { [native code] }」
    if (this && this.__native_code) return this.__native_code;
    return nativeToString.call(this);
  };
  fakeNativeToString.__native_code = 'function toString() { [native code] }';
  Function.prototype.toString = fakeNativeToString;

  // ---- 3. console.table 时间差反检测 ----
  // Boss 用 console.table 渲染耗时判断 DevTools 是否开启
  var origTable = console.table;
  var tableCallCount = 0;
  console.table = function () {
    tableCallCount++;
    // 只在前几次响应（防止 Boss 用调用次数探测 hook）
    if (tableCallCount <= 3) {
      origTable.apply(console, arguments);
    }
  };
  // 伪造 console.table 的 toString
  console.table.__native_code = 'function table() { [native code] }';

  // ---- 4. performance.now() 时间差反检测 ----
  // Boss 用 debugger 命中导致的 performance.now 慢来探测
  var origNow = performance.now.bind(performance);
  var lastNow = origNow();
  performance.now = function () {
    var real = origNow();
    // 加微小随机抖动，让时间差检测失效（不破坏单调性）
    var jitter = (Math.random() - 0.5) * 0.001;
    lastNow = Math.max(lastNow + 0.001, real + jitter);
    return lastNow;
  };
  performance.now.__native_code = 'function now() { [native code] }';

  // ---- 5. 禁用 disable-devtool 检测 ----
  // disable-devtool 通过轮询 debugger/timeDiff 检测
  // 用上述 performance.now + console.table hook 已部分缓解
  // 额外：拦截 debugger 语句（用 Function 构造器包装）
  try {
    var origFunction = window.Function;
    var patchedFunction = function () {
      var args = Array.prototype.slice.call(arguments);
      var body = args[args.length - 1] || '';
      if (typeof body === 'string' && body.indexOf('debugger') !== -1) {
        args[args.length - 1] = body.replace(/debugger/g, '');
      }
      return origFunction.apply(null, args);
    };
    patchedFunction.prototype = origFunction.prototype;
    patchedFunction.__native_code = 'function Function() { [native code] }';
    window.Function = patchedFunction;
  } catch (e) { /* ignore */ }

  // ---- 6. window.chrome 伪造（headless/自动化缺失） ----
  // 真实 Chrome 有 window.chrome 对象，headless 可能缺失
  if (!window.chrome) {
    window.chrome = {
      runtime: {},
      loadTimes: function () { return {}; },
      csi: function () { return {}; },
      app: {},
    };
  }

  // ---- 7. permissions API 伪造 ----
  // 自动化浏览器 Notification.permission 与 query 不一致
  var origQuery = navigator.permissions && navigator.permissions.query;
  if (origQuery) {
    navigator.permissions.query = function (params) {
      if (params && params.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission });
      }
      return origQuery.call(navigator.permissions, params);
    };
    navigator.permissions.query.__native_code = 'function query() { [native code] }';
  }

  // ---- 8. plugins/mimeTypes/languages 伪造（headless 缺失） ----
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: function () {
        return [
          { name: 'Chrome PDF Plugin' },
          { name: 'Chrome PDF Viewer' },
          { name: 'Native Client' },
        ];
      },
      configurable: true,
    });
    Object.defineProperty(navigator, 'languages', {
      get: function () { return ['zh-CN', 'zh', 'en']; },
      configurable: true,
    });
  } catch (e) { /* ignore */ }
})();
