"""``main.py doctor``：一条命令看清运行环境。

存在的理由很具体：OCR 那条「请安装 pytesseract 和 Tesseract OCR」的提示，
真正的原因是**启动用的解释器不是项目 .venv**（系统 python 里有 PIL/mss，
所以应用能起来，但缺 pytesseract）。没有这条命令时，只能靠人肉 ``where python``
和逐个 ``import`` 去猜。这里把解释器、依赖、Tesseract、配置、数据库、日志
一次列全，每条给出 ok/warn/bad 三档。

只读：不会创建、修改或删除任何文件。
"""
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys

from .log_setup import log_path

OK = "ok"
WARN = "warn"
BAD = "bad"

_MARK = {OK: "[ OK ]", WARN: "[WARN]", BAD: "[FAIL]"}

#: 运行时依赖（缺了对应功能会降级，应用本身仍能跑）。
RUNTIME_MODULES = (
    ("PIL", "截图/图片编解码"),
    ("mss", "屏幕抓取"),
    ("pystray", "托盘图标"),
    ("pytesseract", "截图提取文字"),
    ("argon2", "密匣加密"),
    ("cryptography", "密匣加密"),
    ("tkinter", "主面板/工具窗口"),
)

TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def _python_version():
    return f"Python {sys.version.split()[0]} ({sys.maxsize.bit_length() + 1}-bit)"


def _module_status(name, find_spec=importlib.util.find_spec):
    try:
        spec = find_spec(name)
    except (ImportError, ValueError):
        spec = None
    return OK if spec is not None else BAD


def interpreter_rows():
    """解释器与虚拟环境：`python main.py` 打到系统 python 是本次事故的根因。"""
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    rows = [
        ("解释器", sys.executable, OK if in_venv else WARN),
        ("版本", _python_version(), OK),
    ]
    if not in_venv:
        rows.append((
            "虚拟环境",
            "未使用 .venv —— OCR/托盘/密匣会静默降级；"
            "请用 .venv\\Scripts\\pythonw.exe main.py 启动",
            WARN,
        ))
    return rows


def dependency_rows(find_spec=importlib.util.find_spec):
    return [
        (f"依赖 {name}", purpose, _module_status(name, find_spec))
        for name, purpose in RUNTIME_MODULES
    ]


def find_tesseract(exists=os.path.isfile, which=shutil.which):
    for candidate in TESSERACT_CANDIDATES:
        if exists(candidate):
            return candidate
    return which("tesseract")


def probe_tesseract(path, run=subprocess.run):
    """返回 (版本行, 语言列表, 错误文本)。失败时版本/语言为 None。"""
    if not path:
        return None, None, "可执行文件未找到"

    def _call(*args):
        return run([path, *args], capture_output=True, text=True, timeout=20)

    try:
        version = _call("--version")
        if version.returncode != 0:
            detail = (version.stderr or "").strip()[:200]
            return None, None, detail or "tesseract --version 失败"
        first = (version.stdout or "").strip().splitlines()
        langs = _call("--list-langs")
        languages = []
        if langs.returncode == 0:
            languages = [
                line.strip() for line in (langs.stdout or "").splitlines()
                if line.strip() and not line.lower().startswith("list of")
            ]
        return (first[0] if first else "tesseract"), languages, None
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, str(exc)


def tesseract_rows(path, probe):
    if not path:
        return [("Tesseract", "未找到：截图提取文字不可用（装到 "
                              "C:\\Program Files\\Tesseract-OCR 或加入 PATH）", BAD)]
    version, languages, error = probe
    if error:
        return [("Tesseract", f"{path} —— 探针失败：{error}", BAD)]
    rows = [("Tesseract", f"{version} @ {path}", OK)]
    if languages:
        chinese = "chi_sim" in languages
        rows.append((
            "语言包",
            ", ".join(languages) + ("" if chinese else "  ← 缺 chi_sim，只能识别英文"),
            OK if chinese else WARN,
        ))
    else:
        rows.append(("语言包", "未能列出语言", WARN))
    return rows


def _human(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def storage_rows(config, config_path, db_factory=sqlite3.connect):
    rows = []
    if config_path:
        exists = os.path.isfile(config_path)
        rows.append(("配置文件", config_path, OK if exists else BAD))
    db_path = (config or {}).get("db_path")
    if not db_path:
        rows.append(("数据库", "配置里没有 db_path", BAD))
        return rows
    if db_path == ":memory:":
        rows.append(("数据库", ":memory:", OK))
        return rows
    if not os.path.isfile(db_path):
        rows.append(("数据库", f"{db_path}（尚未创建）", WARN))
        return rows
    detail = f"{db_path}  {_human(os.path.getsize(db_path))}"
    wal = db_path + "-wal"
    if os.path.isfile(wal):
        detail += f"  +wal {_human(os.path.getsize(wal))}"
    try:
        conn = db_factory(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM clipboard").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        rows.append(("数据库", f"{detail}  ← 打不开：{exc}", BAD))
        return rows
    rows.append(("数据库", f"{detail}  {count} 条记录", OK))
    return rows


def collect(config=None, config_path=None, log_file=None,
            find_spec=importlib.util.find_spec, exists=os.path.isfile,
            which=shutil.which, run=subprocess.run, db_factory=sqlite3.connect):
    """收集报告行：[(分组, 标签, 值, 状态), ...]。参数可注入，便于测试。"""
    tesseract = find_tesseract(exists=exists, which=which)
    probe = probe_tesseract(tesseract, run=run) if tesseract else (None, None, "未找到")

    rows = []
    for section, items in (
        ("环境", interpreter_rows()),
        ("依赖", dependency_rows(find_spec)),
        ("OCR", tesseract_rows(tesseract, probe)),
        ("存储", storage_rows(config, config_path, db_factory)),
    ):
        rows += [(section, label, value, status) for label, value, status in items]
    rows.append(("存储", "日志", log_file or log_path(), OK))
    return rows


def format_report(rows):
    """把报告行排成对齐的表格；分组之间空一行。"""
    if not rows:
        return ""
    width = max(len(row[1]) for row in rows)
    lines = []
    section_seen = None
    for section, label, value, status in rows:
        if section != section_seen:
            if lines:
                lines.append("")
            lines.append(section)
            section_seen = section
        lines.append(f"  {_MARK.get(status, '[????]')} {label:<{width}}  {value}")
    bad = sum(1 for row in rows if row[3] == BAD)
    warn = sum(1 for row in rows if row[3] == WARN)
    lines.append("")
    lines.append(f"{len(rows)} 项检查：{bad} 项失败，{warn} 项警告")
    return "\n".join(lines)
