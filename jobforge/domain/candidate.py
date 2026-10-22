# -*- coding: utf-8 -*-
"""
求职工作台 · 候选人档案
========================

产品的另一半。前面做的岗位库只是"素材",真正回答用户问题的是
「我 vs 这个岗位」的对比,所以必须先有"我"。

三条纪律：
1. **不做假**。解析不出的技能就留空，绝不用"推测你会"来凑匹配度。
   v1.2.0 的教训是把猜测当事实（字体解码把猜测表当事实输出）。
2. **可核对**。每条技能都保留它在简历里的原句，用户能自己复核。
3. **隐私**。档案只落在本地，绝不外传。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class CandidateLoadError(Exception):
    """档案文件读不了 —— 必须显式抛，不能静默当成"没档案"。

    实测踩过的坑：Windows PowerShell 写文件带 BOM，
    解析失败后代码静默返回空档案，用户看到的是
    「还没有候选人档案」——和自己的文件坏了完全对不上号，很难排查。
    """


# =============================================================================
# 技能词典 —— 中英文混合，按领域分组
# =============================================================================

SKILL_TAXONOMY: Dict[str, List[str]] = {
    "语言": [
        "Java", "Python", "Go", "Golang", "C++", "C#", ".NET", "PHP", "Ruby",
        "Scala", "Kotlin", "Swift", "Objective-C", "Rust", "Dart", "Shell",
        "SQL", "TypeScript", "JavaScript",
    ],
    "后端框架": [
        "Spring", "Spring Boot", "Spring Cloud", "Spring MVC", "MyBatis",
        "MyBatis-Plus", "Hibernate", "Django", "Flask", "FastAPI", "Gin",
        "Express", "NestJS", "Laravel", "Rails", "Netty", "Dubbo", "gRPC",
    ],
    "前端": [
        "React", "Vue", "Angular", "Svelte", "Next.js", "Nuxt", "Webpack",
        "Vite", "小程序", "uni-app", "Flutter", "React Native", "小程序开发",
    ],
    "数据库": [
        "MySQL", "PostgreSQL", "Oracle", "SQL Server", "SQLite", "MongoDB",
        "Redis", "HBase", "ClickHouse", "Elasticsearch", "Neo4j", "TiDB",
        "MariaDB", "Hive", "H2",
    ],
    "中间件": [
        "Kafka", "RabbitMQ", "RocketMQ", "Pulsar", "Nacos", "Zookeeper",
        "Eureka", "Consul", "etcd", "XXL-Job", "Elasticsearch", "Solr",
    ],
    "云原生": [
        "Docker", "Kubernetes", "K8s", "Jenkins", "GitLab CI", "CI/CD",
        "Nginx", "Linux", "Ansible", "Terraform", "Prometheus", "Grafana",
        "Istio", "Helm", "DevOps",
    ],
    "大数据": [
        "Spark", "Flink", "Hadoop", "Hive", "MapReduce", "Kafka Streams",
        "数据仓库", "数据中台", "ETL", "Airflow", "DolphinScheduler",
    ],
    "算法与AI": [
        "机器学习", "深度学习", "PyTorch", "TensorFlow", "NLP", "自然语言处理",
        "计算机视觉", "CV", "推荐系统", "推荐算法", "搜索算法", "大模型",
        "LLM", "RAG", "AIGC", "特征工程", "BERT", "Transformer", "XGBoost",
        "LightGBM", "数据分析", "数据挖掘", "Pandas", "NumPy", "Spark MLlib",
    ],
    "前端工程化": [
        "性能优化", "前端工程化", "组件库", "Node.js", "TypeScript",
        "响应式布局", "跨端", "小程序性能",
    ],
    "架构与设计": [
        "微服务", "分布式", "高并发", "高可用", "分布式事务", "服务治理",
        "领域驱动", "DDD", "缓存设计", "幂等", "限流", "熔断", "降级",
        "架构设计", "技术选型", "性能调优",
    ],
    "测试与质量": [
        "单元测试", "自动化测试", "接口测试", "性能测试", "Selenium",
        "Appium", "JMeter", "测试框架", "CI 测试", "代码质量", "SonarQube",
    ],
    "运维与监控": [
        "运维", "监控告警", "日志分析", "故障排查", "容量规划",
        "Shell 脚本", "Ansible", "Puppet", "Sentry",
    ],
    "产品与设计": [
        "需求分析", "原型设计", "Axure", "Figma", "Sketch", "用户研究",
        "数据看板", "交互设计", "视觉设计", "产品规划", "PRD",
    ],
    "管理与协作": [
        "项目管理", "敏捷开发", "Scrum", "看板", "跨部门协作", "技术分享",
        "带团队", "导师", "沟通能力", "文档撰写",
    ],
    "安全": [
        "渗透测试", "Web 安全", "漏洞修复", "权限设计", "数据加密",
        "等级保护", "安全审计",
    ],
}

# 别名归一：用户简历里写 A，JD 里写 B，要能认出是同一个东西
SKILL_ALIASES: Dict[str, str] = {
    "golang": "Go", "go lang": "Go",
    "springboot": "Spring Boot", "spring-boot": "Spring Boot",
    "spring cloud": "Spring Cloud",
    "k8s": "Kubernetes", "kubernetes": "Kubernetes",
    "postgres": "PostgreSQL", "postgresql": "PostgreSQL",
    "elastic search": "Elasticsearch", "es": "Elasticsearch",
    "reactjs": "React", "react.js": "React",
    "vuejs": "Vue", "vue.js": "Vue",
    "nodejs": "Node.js", "node": "Node.js",
    "pytorch": "PyTorch", "tensorflow": "TensorFlow",
    "nlp": "NLP", "cv": "计算机视觉",
    "微服务架构": "微服务", "分布式架构": "分布式",
    "消息队列": "消息队列", "mq": "消息队列",
    "hadoop生态": "Hadoop",
    "etl开发": "ETL", "数据开发": "数据仓库",
    "机器学习算法": "机器学习",
    "大语言模型": "大模型", "llm应用": "LLM",
}

# 通用词 —— 在 JD 里高频出现但不是技术要求，避免污染匹配度
_STOP_TECH_WORDS = {
    "技术", "开发", "设计", "系统", "平台", "项目", "产品", "业务", "服务",
    "要求", "熟悉", "熟练", "掌握", "具备", "能力", "经验", "优先", "熟练掌握",
    "良好", "扎实", "深入", "理解", "相关", "以上", "本科", "学历", "工作",
}

# 软性素质词 —— JD 里几乎必写，但**不该计入技术匹配度**。
# 实测踩过的坑：「有良好的沟通能力与责任心」被当成技术要求，
# 和 Java/Spring 一起算进匹配度分母，结果简历里没写"沟通能力"就掉分。
# 但用户会困惑：简历上写"沟通能力强"本来就没人信，HR 也不认，
# 凭什么拿它扣我的匹配度？所以这类词单独提示，不参与计算。
SOFT_SKILLS: Dict[str, str] = {
    "沟通能力": "表达与协作",
    "沟通协调": "表达与协作",
    "团队合作": "协作",
    "协作能力": "协作",
    "责任心": "职业态度",
    "责任感": "职业态度",
    "抗压能力": "职业态度",
    "工作压力": "职业态度",
    "执行力": "职业态度",
    "主动性": "职业态度",
    "学习能力": "职业态度",
    "逻辑思维": "思维方式",
    "逻辑清晰": "思维方式",
    "抽象建模": "思维方式",
    "建模能力": "思维方式",
    "系统设计思维": "思维方式",
    "系统设计": "思维方式",
    "独立思考": "思维方式",
    "创新意识": "思维方式",
    "服务意识": "职业态度",
    "保密意识": "职业态度",
    "自驱": "职业态度",
}


def is_soft_skill(name: str) -> bool:
    return name in SOFT_SKILLS


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_/\.（）()【】\[\]]+", "", (s or "")).lower()


def build_skill_index() -> Dict[str, Tuple[str, str]]:
    """返回 {归一化技能: (标准名, 分类)}。"""
    idx: Dict[str, Tuple[str, str]] = {}
    for cat, skills in SKILL_TAXONOMY.items():
        for s in skills:
            idx[_norm(s)] = (s, cat)
            low = s.lower()
            idx[low] = (s, cat)
    for alias, canon in SKILL_ALIASES.items():
        cat = next((c for c, ss in SKILL_TAXONOMY.items() if canon in ss), "其他")
        idx[_norm(alias)] = (canon, cat)
    return idx


SKILL_INDEX = build_skill_index()


def extract_skills(text: str, index: Optional[Dict[str, Tuple[str, str]]] = None
                   ) -> List[Dict[str, Any]]:
    """从一段文本（简历或 JD）里抽取技能，带原文证据。

    返回 [{name, category, evidence, count}]，按出现次数降序。
    **只认词典里的词** —— 不认识的词宁可漏掉，也不用"推测"。
    """
    index = index or SKILL_INDEX
    text = text or ""
    low = text.lower()
    found: Dict[str, Dict[str, Any]] = {}

    for key, (canon, cat) in index.items():
        if not key or len(key) < 2:
            continue
        # 用边界避免 "go" 命中 "google"、"es" 命中 "test" 这类误报
        pattern = r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])"
        hits = list(re.finditer(pattern, low))
        if not hits:
            continue
        ev = ""
        if hits:
            s = max(0, hits[0].start() - 40)
            e = min(len(text), hits[0].end() + 40)
            ev = re.sub(r"\s+", " ", text[s:e]).strip()
        prev = found.get(canon)
        if prev:
            prev["count"] += len(hits)
        else:
            found[canon] = {"name": canon, "category": cat,
                            "evidence": ev, "count": len(hits)}

    return sorted(found.values(), key=lambda x: -x["count"])


# =============================================================================
# 硬性要求识别
# =============================================================================

# 表示"这是硬性要求"的措辞
_HARD_PATTERNS = [
    r"必须", r"精通", r"熟练掌握", r"熟练使用", r"熟练运用", r"扎实",
    r"深刻理解", r"深入理解", r"深度理解", r"required", r"必须熟悉",
]
# 表示"这是加分项"的措辞
_NICE_PATTERNS = [
    r"优先", r"加分", r"更佳", r"最好", r"有.{0,4}经验者优先", r"plus", r"bonus",
    r"了解.{0,6}(即可|优先)",
]

_HARD_SPLIT = re.compile("|".join(_HARD_PATTERNS))
_NICE_SPLIT = re.compile("|".join(_NICE_PATTERNS))


def _requirement_level(sentence: str) -> str:
    """判断一句要求是 hard / nice / normal。"""
    if _NICE_SPLIT.search(sentence):
        return "nice"
    if _HARD_SPLIT.search(sentence):
        return "hard"
    return "normal"


def split_sentences(text: str) -> List[str]:
    parts = re.split(r"[。；;\n\r]+|(?<=[，,])\s*(?=[^，,]{12,})", text or "")
    return [re.sub(r"\s+", " ", p).strip() for p in parts if p and p.strip()]


# =============================================================================
# 候选人档案
# =============================================================================

@dataclass
class Candidate:
    name: str = ""
    email: str = ""
    phone: str = ""
    target_city: str = ""
    target_positions: List[str] = field(default_factory=list)
    salary_expect: str = ""

    # 从简历解析出的能力
    skills: List[Dict[str, Any]] = field(default_factory=list)
    skill_names: List[str] = field(default_factory=list)
    education: str = ""
    experience_years: float = 0.0
    is_fresh_grad: bool = True
    resume_text: str = ""

    created_at: str = ""
    updated_at: str = ""

    def finalize(self) -> "Candidate":
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self.created_at = self.created_at or now
        self.updated_at = now
        self.skill_names = [s["name"] for s in self.skills]
        return self

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_resume(cls, text: str, name: str = "") -> "Candidate":
        """从简历全文建档案。

        注意：**只提取明确写出来的技能**。没写的技能就是不会，
        不用"看起来可能会"来加分 —— 那是虚假匹配，比不匹配更糟。
        """
        text = text or ""
        c = cls(name=name, resume_text=text)
        c.skills = extract_skills(text)
        c.education = _guess_education(text)
        c.experience_years = _guess_years(text)
        c.is_fresh_grad = _guess_fresh(text)
        c.target_positions = [s["name"] for s in c.skills[:3]]
        return c.finalize()

    # ---- 持久化 ----

    @classmethod
    def load(cls, path: str = "data/candidate.json") -> "Candidate":
        """读档案。

        用 ``utf-8-sig`` 而不是 ``utf-8``：Windows 上 PowerShell 的
        ``Set-Content -Encoding UTF8`` 会写 BOM，用普通 utf-8 读会抛
        ``JSONDecodeError``。

        实测踩过的坑：文件带了 BOM → 解析失败 → 这里静默返回空档案 →
        用户看到的是"还没有候选人档案"，完全不知道是自己的文件坏了。
        所以**解析失败必须让调用方知道**，不能装作文件不存在。
        """
        p = Path(path)
        if not p.exists():
            return cls()
        raw = p.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CandidateLoadError(
                f"候选人档案解析失败（{path}）：{e}。"
                "文件可能是空的或被手工改坏了，请删除后重新用 "
                "`python -m jobforge.cli profile --file 简历.txt` 生成。"
            ) from e
        if not isinstance(data, dict):
            raise CandidateLoadError(
                f"候选人档案格式不对（{path}）：顶层不是对象")
        return cls.from_dict(data)

    def save(self, path: str = "data/candidate.json") -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return str(p)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Candidate":
        known = {f for f in cls.__dataclass_fields__}  # noqa: F821
        return cls(**{k: v for k, v in d.items() if k in known})


_EDU_MAP = [("博士", 5), ("硕士", 4), ("研究生", 4), ("本科", 3),
            ("大专", 2), ("专科", 2), ("高中", 1)]


def _guess_education(text: str) -> str:
    for word, _ in _EDU_MAP:
        if word in text:
            return "硕士" if word == "研究生" else word
    return ""


def _guess_years(text: str) -> float:
    m = re.findall(r"(\d+)\s*年(?:以上)?\s*(?:工作|开发|从业|研发)?经[验历]", text)
    m += re.findall(r"(\d+)\s*年\s*(?:以上)?\s*经验", text)
    return max([float(x) for x in m], default=0.0)


def _guess_fresh(text: str) -> bool:
    if any(k in text for k in ("应届", "在校", "校招", "毕业", "实习生")):
        return True
    if _guess_years(text) >= 1.5:
        return False
    return True
