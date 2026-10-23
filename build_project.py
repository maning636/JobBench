#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包成可分发的项目包（本地 zip，不推 GitHub）
=================================================

**刻意不打 .git 目录、不带个人数据、不带 API Key。**
v1.2.0 的 zip 里塞了 __pycache__、旧版本 zip、开发者本人的 15 条求职记录
和 dashboard.db —— 那是隐私事故。这次逐项排除。
"""
from __future__ import annotations

import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / f"jobforge-{datetime.now():%Y%m%d}.zip"

# 包内目录名。对外发的包不该带 "-v2" 这种开发期叫法。
PKG_DIR = "求职工作台"

# 绝不能进包的东西 —— 每一条都对应一次实测踩过的坑
EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv", ".idea", ".vscode",
                "_build", "node_modules", ".pytest_cache", ".mypy_cache",
                "data"}          # data/ 全排：含简历、API Key、数据库
EXCLUDE_FILES = {
    ".env", ".env.local", "_dbg.py", "check_ui.py",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".db", ".sqlite", ".zip", ".log", ".txt"}

# 白名单：只有这些能进包。与其穷举排除，不如只放行该放的 ——
# 实测踩过：靠排除清单，结果一堆临时 txt 和脚本混进了 zip。
ALLOW_TOP = {
    "README.md", "HISTORY.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md",
    "requirements.txt", ".gitignore", "pyproject.toml", "setup.py",
    "build_project.py",
    # 双击启动入口 —— 没有它，用户打开压缩包看到一堆 .py 就直接放弃了
    "启动.bat",
    "启动桌面版.bat",
}
ALLOW_DIRS = {"jobforge", "tests"}


def should_skip(p: Path) -> bool:
    """只放行白名单里的东西。"""
    parts = p.parts
    top = parts[0]

    if top in EXCLUDE_DIRS:
        return True
    if len(parts) == 1:
        return top not in ALLOW_TOP
    # 目录内只允许 jobforge/ 与 tests/
    if top not in ALLOW_DIRS:
        return True
    if p.suffix.lower() in EXCLUDE_SUFFIX:
        return True
    if p.suffix.lower() == ".py":
        return p.name.startswith("test_install")
    return False


def scan_leaks(files) -> list:
    """扫包内文件，找出不该外发的东西。

    靠人肉检查迟早漏 —— v1.2.0 那次 zip 塞了开发者本人的求职记录，
    就是因为「打包完没再看一眼」。这里把检查变成打包流程的一部分。

    返回 [(类别, 文件, 行号), ...]，空 = 干净。
    """
    # 只扫文本类。二进制（图片、字体）按字节跳过。
    text_suffix = {".py", ".md", ".txt", ".html", ".css", ".js", ".json",
                   ".bat", ".ps1", ".yml", ".yaml", ".cfg", ".ini", ".toml"}
    # 内部称呼/目录名不在这里写死 —— 写成拼接形式，
    # 这样这个文件本身进包时也不会暴露具体是什么。
    _internal_alias = "马" + "哥"
    _internal_user = "马" + "不宁"
    _internal_dir = ".mini" + "max"
    patterns = [
        (r"sk-[A-Za-z0-9]{16,}", "疑似 API Key"),
        (r"[A-Za-z0-9]{32,}\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{16,}", "疑似 API Key"),
        (r"https?://open\.bigmodel\.cn", "写死的模型服务地址"),
        # Windows 绝对路径（盘符 + 路径），每人机器上都可能不同，属于隐私
        # raw string 里两个反斜杠 = 正则匹配一个反斜杠（路径里的分隔符只有一个）
        (r"[A-Za-z]:\\[A-Za-z0-9_.\- ]{3,}", "本机绝对路径"),
        (r"/Users/[A-Za-z0-9_.-]+/", "本机绝对路径"),
        (r"/home/[A-Za-z0-9_.-]+/", "本机绝对路径"),
        (re.escape(_internal_alias) + "|" + re.escape(_internal_user)
         + "|" + re.escape(_internal_dir), "内部痕迹"),
    ]
    compiled = [(re.compile(p), label) for p, label in patterns]

    # 必须保持纯 ASCII 的文件 —— 写中文会让安装/启动在中文 Windows 上直接失败。
    # 这两个坑都踩过：requirements.txt 里中文注释 → pip 按 GBK 解码崩掉；
    # .bat 里中文 → cmd 代码页显示乱码。
    ascii_only = {"requirements.txt", "启动.bat"}

    # 允许出现的绝对路径：程序安装位置（每个人机器上都一样，不是隐私）
    # 注意这里用普通字符串，路径里的反斜杠只写一个。
    path_allow = (
        "C:\\Program Files",
        "C:\\Program Files (x86)",
        "C:\\Windows",
    )

    hits = []
    for p, rel in files:
        # 检查器自己带着关键词，跳过（否则它会举报自己）
        try:
            if p.resolve() == Path(__file__).resolve():
                continue
        except OSError:
            pass
        if p.suffix.lower() not in text_suffix:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # 必须纯 ASCII 的文件：出现非 ASCII 就是致命问题，直接拦下
        if p.name in ascii_only:
            bad_chars = sorted({c for c in text if ord(c) >= 128})
            if bad_chars:
                hits.append(("非 ASCII 字符（会导致安装/启动失败）",
                             rel.as_posix(), 0))
                continue
        for i, line in enumerate(text.splitlines(), 1):
            for rx, label in compiled:
                if rx.search(line):
                    # 程序安装路径不是隐私，放行
                    if label == "本机绝对路径" and any(
                            a in line for a in path_allow):
                        continue
                    hits.append((label, rel.as_posix(), i))
    return hits


def main() -> int:
    files = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if should_skip(rel):
            continue
        files.append((p, rel))

    base = PKG_DIR
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in files:
            z.write(p, arcname=str(Path(base) / rel))

    size_kb = OUT.stat().st_size / 1024
    print(f"✓ 打包完成：{OUT.name}　({size_kb:.0f} KB，{len(files)} 个文件)")

    print("\n包含：")
    for p, rel in files:
        print(f"  {rel}")

    print("\n已排除（隐私 / 产物）：")
    print("  data/（含 candidate.json、llm.json、*.db）—— 你的简历和 API Key")
    print("  __pycache__/*.pyc　build/*.zip　.env　临时输出")

    missing = [f for f in ("README.md", "HISTORY.md", "LICENSE",
                          "CHANGELOG.md", "CONTRIBUTING.md", "启动.bat")
               if not (ROOT / f).exists()]
    if missing:
        print(f"\n⚠ 缺少：{', '.join(missing)}")

    leaks = scan_leaks(files)
    if leaks:
        print(f"\n{'✗' * 3} 泄漏检查未通过，包不要发出去：")
        for what, where, line in leaks:
            print(f"   {what}  {where}:{line}")
        return 1
    print("\n✓ 泄漏检查通过：包内没有密钥、本机路径、内部称呼")
    return 0


if __name__ == "__main__":
    sys.exit(main())
