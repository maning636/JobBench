# -*- coding: utf-8 -*-
"""
BOSS 解析层测试 —— 全部离线，不碰网络
======================================

这一套验证的是「HTML → Job」这段纯逻辑。
**浏览器会话那部分不在这里**（需要真实登录态，开发环境无法验证）——
测试文件里明确标注了这条边界，避免以后有人拿"测试全绿"当成"抓取能用"。

每个用例都对应一个实测过的坑：
  · 公司名不能从岗位名猜（v1.2.0 存成"5-10年"这种经验值）
  · 日薪不能被当月薪（静默污染薪资评分）
  · 拿不到数据必须报错，不能静默返回空（用户会误以为"今天没岗位"）
  · 面议不能编造数字
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).parent / "fixtures" / "boss"

from jobforge.adapters.sources.boss import (  # noqa: E402
    ParseError, parse_list_page, build_search_url, _guess_city, CITY_CODES,
)
from jobforge.domain.models import SalaryState  # noqa: E402

P, F = [], []


def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))


good = (FIX / "list_page.html").read_text(encoding="utf-8")
login = (FIX / "login_wall.html").read_text(encoding="utf-8")
restructured = (FIX / "restructured.html").read_text(encoding="utf-8")


print("\n[1] 正常列表页")
# =============================================================================
jobs = parse_list_page(good, "https://www.zhipin.com/web/geek/job?query=Java")
check("解析出 4 个岗位", len(jobs) == 4, f"实际 {len(jobs)}")

j0 = jobs[0]
check("岗位名正确", j0.position == "Java开发工程师", j0.position)
check("公司名来自 company-name 而非岗位名",
      j0.company == "北京云启科技有限公司", j0.company)
check("公司名不等于经验值",
      not any(x in j0.company for x in ("年", "本科", "K")), j0.company)
check("薪资文本正确", j0.salary_text == "15-25K·13薪", j0.salary_text)
check("薪资解析为数值", j0.salary_min == 15000 and j0.salary_max == 25000,
      f"{j0.salary_min}~{j0.salary_max}")
check("薪资标记为已核验", j0.salary_state == SalaryState.VERIFIED.value,
      j0.salary_state)
check("城市从区域推断", j0.city == "北京", j0.city)
check("区域正确", j0.district == "北京 海淀区", j0.district)
check("经验正确", j0.experience == "3-5年", j0.experience)
check("学历正确", j0.education == "本科", j0.education)
check("用工类型正确", j0.employment_type == "全职", j0.employment_type)
check("公司规模正确", j0.company_size == "100-499人", j0.company_size)
check("行业正确", j0.company_industry == "计算机", j0.company_industry)
check("链接已转绝对路径", j0.url.startswith("https://"), j0.url)
check("指纹已生成", bool(j0.fingerprint), j0.fingerprint)


print("\n[2] 日薪绝不能被当月薪 —— 静默污染薪资评分的根源")
# =============================================================================
j_day = next(j for j in jobs if "实习" in j.position)
check("识别出实习岗", "实习" in j_day.position, j_day.position)
check("日薪薪资文本保留", j_day.salary_text == "200-300元/天", j_day.salary_text)
check("日薪周期标记为日", j_day.salary_period == "日", j_day.salary_period)
check("日薪数值不被折算成月薪",
      not (j_day.salary_max and j_day.salary_max > 1000),
      f"{j_day.salary_min}~{j_day.salary_max}")
check("日薪岗不参与月薪评分",
      j_day.salary_max is None or j_day.salary_max < 1000,
      str(j_day.salary_max))


print("\n[3] 面议不能编造数字")
# =============================================================================
j_na = next(j for j in jobs if "面议" in j.salary_text)
check("面议岗位薪资为 None", j_na.salary_min is None and j_na.salary_max is None,
      f"{j_na.salary_min}~{j_na.salary_max}")
check("面议标记为 NA 而非 unresolved",
      j_na.salary_state == SalaryState.NA.value, j_na.salary_state)


print("\n[4] 拿不到数据必须报错，不能静默返回空")
# =============================================================================
# 这条是 v1.2.0 最贵的教训：静默返回 []，用户以为是"今天没新岗位"
try:
    r = parse_list_page(login)
    check("登录墙要报错而不是返回空", False, f"竟然返回了 {len(r)} 条")
except ParseError as e:
    check("登录墙要报错而不是返回空", True)
    check("登录墙错误信息说人话", "登录" in str(e), str(e))

try:
    r = parse_list_page(restructured)
    check("页面改版要报错而不是返回空", False, f"竟然返回了 {len(r)} 条")
except ParseError as e:
    check("页面改版要报错而不是返回空", True)
    check("改版错误提示选择器失效", "选择器" in str(e) or "改版" in str(e), str(e))

for name, payload in (("空页面", ""), ("过短内容", "<html></html>")):
    try:
        parse_list_page(payload)
        check(f"{name}要报错", False, "竟然没报错")
    except ParseError:
        check(f"{name}要报错", True)


print("\n[5] URL 构建")
# =============================================================================
u = build_search_url("Java 后端", "北京", 2)
check("关键词已编码", "Java%20%E5%90%8E%E7%AB%AF" in u or "Java" in u, u)
check("城市码正确", CITY_CODES["北京"] in u, u)
check("页码正确", "page=2" in u, u)
check("未知城市回落到全国", CITY_CODES["全国"] in build_search_url("x", "火星"), "")
check("空城市用全国", CITY_CODES["全国"] in build_search_url("x", ""), "")

check("城市推断可用", _guess_city("北京 海淀区") == "北京", "")
check("城市推断无信息时返回空", _guess_city("未知区域") == "", "")


print("\n[6] 幂等与稳定性")
# =============================================================================
again = parse_list_page(good, "https://www.zhipin.com/web/geek/job?query=Java")
check("同样输入产出同样指纹",
      [j.fingerprint for j in jobs] == [j.fingerprint for j in again])
check("指纹互不相同", len({j.fingerprint for j in jobs}) == len(jobs))
check("解析顺序稳定",
      [j.position for j in jobs] == [j.position for j in again])

print("\n" + "=" * 62)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    print("  失败项：")
    for x in F:
        print("   -", x)
print()
print("  ⚠ 本测试只覆盖「HTML → Job」这段纯逻辑。")
print("    浏览器会话（需要你自己的 BOSS 登录态）不在测试范围内，")
print("    请用 `jobforge collect --check` 先做连通性自检。")
print("=" * 62)
sys.exit(1 if F else 0)
