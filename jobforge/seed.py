# -*- coding: utf-8 -*-
"""
求职工作台 · 种子数据
======================

用途：让新用户第一次打开就能看到界面长什么样，也是开发时的回归基线。

**这些数据是为演示构造的**，格式模仿 BOSS 真实复制文本的形态，
用于验证解析链路与界面，不代表真实在招岗位。

每条都故意覆盖一种边界情况，让界面和数据健康条有东西可展示：
  - 正常校招岗 / 社招岗
  - 信息不全（缺公司名）
  - 疑似外包（带证据）
  - 面议薪资（不编数字）
  - 混合城市与学历
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.adapters.sources.manual import (
    parse_boss_text, import_from_csv, import_from_text,
)
from jobforge.persistence.sqlite_store import Store
from jobforge.domain.models import apply_salary

SEED_TEXT = """Java开发工程师
15-25K·13薪
北京云启科技有限公司
北京 海淀区 3-5年 本科
负责核心交易链路后端服务的设计与开发，参与微服务架构演进；熟悉 MySQL、Redis、Kafka。

算法工程师（校招）
20-35K
上海启明智能科技有限公司
上海 浦东新区 在校/应届 硕士
负责推荐召回与排序模型的设计和迭代，有顶会论文者优先；应届或一年内毕业均可。

前端开发工程师
12-20K
杭州星辰数据服务有限公司
杭州 余杭区 1-3年 本科
负责数据可视化平台前端开发，要求熟悉 React、TypeScript。

人力资源专员
8-12K
广州卓越实业有限公司
广州 天河区 经验不限 本科
负责校园招聘全流程，包括宣讲会、简历筛选与面试安排。

测试开发工程师
10-16K
深圳智联信息技术有限公司
深圳 南山区 1-3年 大专
负责自动化测试平台建设与接口测试用例设计。

运维工程师
13-22K
成都数云科技有限公司
成都 高新区 3-5年 本科
负责生产环境容器化部署与监控告警建设。

数据分析师
11-18K
武汉光谷智能科技有限公司
武汉 光谷 1-3年 本科
负责经营数据看板搭建与业务分析，SQL 熟练。

产品经理（校招）
10-18K
南京智享网络科技有限公司
南京 雨花台区 应届 本科
负责 C 端产品需求分析与原型设计，具备逻辑分析与表达能力。

UI设计师
9-15K
苏州美序设计有限公司
苏州 工业园区 经验不限 大专
负责产品界面视觉设计与交互规范建设。

后端开发工程师（派遣）
13-20K
北京博远人力资源服务有限公司
北京 昌平区 3-5年 本科
由甲方派遣驻场，负责支付系统后端开发，长期驻场。

运营专员
7-10K
长沙新媒运营有限公司
长沙 岳麓区 经验不限 大专
负责内容账号日常运营与数据复盘。

解决方案工程师
面议
北京鼎信数字科技有限公司
北京 朝阳区 3-5年 本科
负责行业解决方案输出与客户技术交流。"""

SEED_CSV = """岗位名,公司,薪资,城市,学历,经验,公司规模,行业,链接
Java开发实习生,北京云启科技有限公司,200-300元/天,北京,本科,在校/应届,100-499人,互联网,https://example.com/j1
数据分析实习生,上海启明智能科技有限公司,180-260元/天,上海,本科,在校/应届,500-999人,人工智能,https://example.com/j2
测试工程师,深圳智联信息技术有限公司,9-14K,深圳,大专,1-3年,100-499人,计算机,https://example.com/j3
产品助理,广州卓越实业有限公司,7-11K,广州,本科,经验不限,20-99人,电商,https://example.com/j4"""


def seed(db_path: str = "data/jobforge.db", reset: bool = True) -> None:
    st = Store(db_path)
    if reset:
        st.clear()

    jobs, errors = import_from_text([b for b in SEED_TEXT.split("\n\n") if b.strip()])
    if errors:
        print("文本解析告警:", errors)

    # CSV 里的岗位补上 JD 摘要，贴近真实数据形态
    csv_jobs, csv_errors = import_from_csv(SEED_CSV)
    for j in csv_jobs:
        j.jd = f"{j.position}，负责{j.city}区域的日常业务支持与交付。"
        j.employment_type = "实习" if "实习" in j.position else "全职"
        j.finalize()
    if csv_errors:
        print("CSV 解析告警:", csv_errors)

    all_jobs = jobs + csv_jobs
    rep = st.upsert_many(all_jobs)

    print(f"入库完成：接收 {rep.received} · 新增 {rep.inserted} · 更新 {rep.updated}")
    print(f"加权完整率 {rep.completeness:.0%} · 必填缺失 {rep.incomplete} 条")
    print(f"健康判定 {'通过' if rep.healthy else '不通过'} {rep.warnings or ''}")
    print()
    for j in st.list_jobs(limit=100):
        sal = j.salary_text or "(空)"
        print(f"  {j.position[:24]:<26} {j.company[:20]:<22} {sal:<14} "
              f"{j.city:<5} {j.education:<4} 完整度 {j.completeness():.0%}")


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "data/jobforge.db")
