# -*- coding: utf-8 -*-
"""
静态检查：JS 里引用的 #id 是否都真实存在于模板中
=====================================================

实测踩过：首页重做后移除了统计卡，loadTracking 还在写 `#sOpen`。
它抛 TypeError → 列表渲染中断 → 页面表现为「统计有数但列表空白」，
错误只在 console 里。这类问题靠肉眼很难发现，所以做成自动检查。
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "jobforge" / "web" / "static" / "app.js"
HTML = ROOT / "jobforge" / "web" / "templates" / "index.html"

js = JS.read_text(encoding="utf-8")
html = HTML.read_text(encoding="utf-8")

html_ids = set(re.findall(r'\bid="([^"]+)"', html))

# 只取形如 $('#xxx') / $("#xxx") 的选择器，避开动态拼接
used = set(re.findall(r"""\$\(\s*['"]#([A-Za-z_][\w-]*)['"]""", js))

# 拼接式 id：$('#wz' + WIZ.step + 'state') → 运行时才确定，
# 静态正则会误报成 '#wz'。实测踩过这个假警。
dynamic_prefixes = set(re.findall(r"""\$\(\s*['"]#([A-Za-z_][\w-]*)\s*['"]\s*\+""", js))
for pfx in dynamic_prefixes:
    used = {u for u in used if u != pfx}
print(f"（检测到 {len(dynamic_prefixes)} 个拼接式 id 选择器：{', '.join('#'+p for p in dynamic_prefixes) or '无'}）")

# 这些 id 是运行时才创建的（抽屉内容、跟进表单），不在模板里属于正常
RUNTIME_CREATED = {
    "draftBox",     # 投递详情里的跟进话术区
    "fContent",     # 手动记跟进时插入的 textarea
    "fReply",       # 手动记跟进时插入的 input
    "drawer",       # 抽屉类名拼接前缀（drawerImport / drawerApp / ...）
}

missing = sorted(used - html_ids - RUNTIME_CREATED)
unused = sorted(html_ids - used)

print(f"模板里的 id：{len(html_ids)} 个")
print(f"JS 引用的 id：{len(used)} 个")
print()
if missing:
    print("❌ JS 引用了模板里不存在的 id（会抛错并中断渲染）：")
    for m in missing:
        print(f"   #{m}")
else:
    print("✅ JS 引用的 id 全部存在")

# ── 启动入口必须存在 ──────────────────────────────────────────
# 实测踩过：用正则批量替换函数名时把启动调用一起删了，
# 页面不报错但什么都不加载 —— DOM 检查抓不到，必须显式断言。
startup = re.findall(
    r"^(?:showTab\(|loadToday\(|load\(\)|loadLLM\()", js, flags=re.M)
# 启动入口：取「最后一个顶层绑定」之前的区域，而不是简单取末尾 N 字符
# （实测踩过：末尾还有自检代码，会造成假警）
tail = js[js.rfind("$('#importText')"):]
needed = ["showTab(", "loadToday()", "load()", "loadLLM()"]
missing_startup = [n for n in needed if n not in tail]
if missing_startup:
    print(f"❌ 启动入口缺失：{', '.join(missing_startup)}")
    print("   页面不会报错，但除了首屏骨架什么都不加载")
    sys.exit(1)
else:
    print("✅ 启动入口完整（showTab / loadToday / load / loadLLM）")
if unused:
    print()
    print("ℹ  模板里有但 JS 没直接引用的 id（可能是 data-k 或由 HTML 内联调用）：")
    print("   " + ", ".join("#" + u for u in unused))

sys.exit(1 if missing else 0)
