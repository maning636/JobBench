# -*- coding: utf-8 -*-
"""
智联招聘解析层测试 —— 全部离线，不碰网络
==========================================

这一套验证的是「智联返回的数据 → Job」这段纯逻辑。
**浏览器会话那部分不在这里**（需要真实浏览器 + 平台环境，
开发环境无法端到端验证）—— 测试文件里明确标注了这条边界，
避免以后有人拿"测试全绿"当成"抓取能用"。

每个用例都对应一个实测过的坑：
  · 城市必须用数字码：传中文名**不报错**，但静默返回别的城市的数据
  · 薪资七成是「万」写法：不认「万」等于七成岗位薪资解析不出来
  · 拿不到数据必须报错，不能静默返回空（用户会误以为"今天没岗位"）
  · 页面里内联的 JSON 只有第一页，翻页必须走接口
  · JD 正文里带 HTML 标签，不剥会被当成技能词
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).parent / "fixtures" / "zhaopin"

from jobforge.adapters.sources.zhaopin import (  # noqa: E402
    ParseError, parse_search_page, parse_positions_response, positions_count,
    resolve_city_code, build_positions_body, build_search_url, strip_html,
    CITY_CODES,
)
from jobforge.domain.models import SalaryState  # noqa: E402

P, F = [], []


def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))


page_html = (FIX / "search_page.html").read_text(encoding="utf-8")
api_payload = json.loads((FIX / "positions_response.json").read_text(encoding="utf-8"))


print("\n[1] 页面内联 JSON 解析（SSR 那条路）")
# =============================================================================
jobs = parse_search_page(page_html, "https://www.zhaopin.com/jobs/?jl=489&kw=Java&kt=3")
check("解析出 3 个岗位", len(jobs) == 3, f"实际 {len(jobs)}")

j0 = jobs[0]
check("岗位名正确", j0.position == "Java（资金、金融项目）", j0.position)
check("公司名正确",
      j0.company == "软通动力信息技术(集团)股份有限公司", j0.company)
check("薪资文本取自 salary60", j0.salary_text == "9000-10000元", j0.salary_text)
check("薪资解析为数值", j0.salary_min == 9000 and j0.salary_max == 10000,
      f"{j0.salary_min}~{j0.salary_max}")
check("薪资标记为已核验", j0.salary_state == SalaryState.VERIFIED.value,
      j0.salary_state)
check("城市正确", j0.city == "武汉", j0.city)
check("区域正确", j0.district == "洪山", j0.district)
check("经验正确", j0.experience == "3-5年", j0.experience)
check("学历正确", j0.education == "大专", j0.education)
check("公司规模正确", j0.company_size == "10000人以上", j0.company_size)
check("行业正确", j0.company_industry == "软件/IT服务", j0.company_industry)
check("雇佣类型正确", j0.employment_type == "全职", j0.employment_type)
check("岗位详情 URL 正确",
      j0.url == "https://www.zhaopin.com/jobdetail/CC000544460J40851857216.htm",
      j0.url)
check("平台标记为智联", j0.channel == "zhilian", j0.channel)
check("外部 ID 取自岗位号",
      j0.external_id == "CC000544460J40851857216", j0.external_id)
check("JD 非空", len(j0.jd) > 20, f"{len(j0.jd)} 字符")
check("JD 里没有残留 HTML 标签",
      "<div" not in j0.jd and "<p>" not in j0.jd, j0.jd[:80])
check("★ JD 取自嵌套 desc（顶层 jobDescription 接口里恒为空）",
      "五险一金" in j0.jd, j0.jd[:60])


print("\n[2] 接口返回解析（翻页那条路）")
# =============================================================================
jobs2 = parse_positions_response(api_payload)
check("接口也解析出 3 个岗位", len(jobs2) == 3, f"实际 {len(jobs2)}")
check("两条路结果一致（同样字段同样结果）",
      [(x.position, x.company, x.salary_text, x.city, x.url)
       for x in jobs] == [(x.position, x.company, x.salary_text, x.city, x.url)
                          for x in jobs2])
check("总条数能读出来", positions_count(api_payload) == 100,
      str(positions_count(api_payload)))


print("\n[3] 「万」单位薪资 —— 智联七成是这种写法")
# =============================================================================
j2 = jobs2[2]
check("第 3 条是万格式", "万" in j2.salary_text, j2.salary_text)
check("万单位解析成元", j2.salary_min == 15000 and j2.salary_max == 30000,
      f"{j2.salary_min}~{j2.salary_max}")
check("万单位标记为已核验", j2.salary_state == SalaryState.VERIFIED.value,
      j2.salary_state)
check("万单位周期是月", j2.salary_period == "月", j2.salary_period)


print("\n[4] 城市码 —— 传中文名会静默返回错城市的数据")
# =============================================================================
check("北京 → 530", resolve_city_code("北京") == "530", resolve_city_code("北京"))
check("上海 → 538", resolve_city_code("上海") == "538", resolve_city_code("上海"))
check("深圳 → 765", resolve_city_code("深圳") == "765", resolve_city_code("深圳"))
check("直接传数字码也认（智联原生格式）", resolve_city_code("530") == "530",
      resolve_city_code("530"))
check("全国 → 空串（不传 jl）", resolve_city_code("全国") == "")
check("空城市 → 空串", resolve_city_code("") == "")
check("带「市」后缀也能认", resolve_city_code("北京市") == "530",
      resolve_city_code("北京市"))
check("内置城市数量够用", len(CITY_CODES) > 400, str(len(CITY_CODES)))

try:
    resolve_city_code("火星")
    check("不存在的城市必须报错（不能静默用全国）", False, "没有抛异常")
except ParseError as e:
    check("不存在的城市必须报错（不能静默用全国）", "火星" in str(e), str(e))

try:
    resolve_city_code("999999")
    check("未知数字码也必须报错", False, "没有抛异常")
except ParseError:
    check("未知数字码也必须报错", True)


print("\n[5] 请求体 —— 参数名是智联自己的，不能自己编")
# =============================================================================
body = build_positions_body("Java", "北京", page=2)
check("关键词用 S_SOU_FULL_INDEX", body.get("S_SOU_FULL_INDEX") == "Java",
      str(body.get("S_SOU_FULL_INDEX")))
check("城市用 S_SOU_WORK_CITY", body.get("S_SOU_WORK_CITY") == "530",
      str(body.get("S_SOU_WORK_CITY")))
check("页码传 pageIndex", body.get("pageIndex") == 2, str(body.get("pageIndex")))
check("每页 20 条", body.get("pageSize") == 20, str(body.get("pageSize")))
check("platform=13", body.get("platform") == 13, str(body.get("platform")))
check("没有用 kw/jl 这类自己编的参数名",
      "kw" not in body and "jl" not in body, str(sorted(body)))

body_nocity = build_positions_body("Java", "", page=1)
check("全国时不传城市参数", "S_SOU_WORK_CITY" not in body_nocity,
      str(sorted(body_nocity)))

url = build_search_url("Java", "北京")
check("搜索页 URL 带数字城市码", "jl=530" in url, url)
check("搜索页 URL 带关键词", "kw=Java" in url, url)


print("\n[6] 拿不到数据必须报错，不能静默返回空")
# =============================================================================
try:
    parse_search_page("", "https://www.zhaopin.com/jobs/")
    check("空页面必须报错", False, "没有抛异常")
except ParseError:
    check("空页面必须报错", True)

try:
    parse_search_page("<html><body>" + ("没有状态数据的普通页面 " * 40) +
                      "</body></html>", "")
    check("页面结构变了必须报错", False, "没有抛异常")
except ParseError as e:
    check("页面结构变了必须报错", "改版" in str(e) or "状态" in str(e), str(e))

try:
    parse_positions_response({"code": 500, "message": "系统繁忙"})
    check("接口 code != 200 必须报错", False, "没有抛异常")
except ParseError as e:
    check("接口 code != 200 必须报错", "500" in str(e), str(e))

try:
    parse_positions_response({"code": 200, "data": {"count": 0}})
    check("接口没有 list 必须报错", False, "没有抛异常")
except ParseError:
    check("接口没有 list 必须报错", True)

empty = parse_positions_response({"code": 200, "data": {"list": [], "count": 0}})
check("空列表返回空（翻到底了是正常结束，不是错误）", empty == [])


print("\n[7] JD 清洗")
# =============================================================================
check("去掉 div 标签",
      strip_html("<div>第一段</div><div>第二段</div>") == "第一段\n第二段",
      repr(strip_html("<div>第一段</div><div>第二段</div>")))
check("br 变成换行", "a\nb" == strip_html("a<br>b"), repr(strip_html("a<br>b")))
check("实体被还原", strip_html("A&amp;B") == "A&B", repr(strip_html("A&amp;B")))
check("空白被压掉", strip_html("  多  空格  ") == "多 空格",
      repr(strip_html("  多  空格  ")))


print("\n[8] 标签提取 —— 智联有两种形态")
# =============================================================================
tags = jobs[0].tags
check("标签非空", len(tags) > 0, str(tags))
check("没有重复", len(tags) == len(set(tags)), str(tags))


# =============================================================================
print("\n" + "=" * 66)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    print("  失败项：")
    for x in F:
        print("    -", x)
print("=" * 66)

print("\n  ℹ  本测试只覆盖「智联返回的数据 → Job」这段纯逻辑。")
print("    浏览器会话（页面内 POST 接口）不在测试范围内 ——")
print("    那部分需要真实 Chrome，智联也会对高频请求做风控。")

sys.exit(1 if F else 0)
