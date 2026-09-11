"""文本内容分类：链接 / 代码 / 普通文本。

规则保守优先：宁可归为普通文本，也不把日常文字误判成代码。
链接要求整段内容（或每一行）都是 URL；代码用多信号计分。
"""
import re

_SCHEME_RE = re.compile(r"^(https?|ftp|file)://\S+$", re.I)
_MAILTO_RE = re.compile(r"^mailto:\S+@\S+$", re.I)
_WWW_RE = re.compile(r"^www\.\S+$", re.I)
_DOMAIN_RE = re.compile(
    r"^[a-z0-9][a-z0-9.\-]*\.([a-z]{2,})(:\d+)?(/\S*)?(\?\S*)?(#\S*)?$", re.I)

# 裸域名（无 scheme/www）只认常见 TLD，避免 report.pdf、v1.2 之类误判
_COMMON_TLDS = {
    "com", "cn", "net", "org", "io", "co", "gov", "edu", "info", "biz",
    "me", "tv", "dev", "app", "ai", "xyz", "top", "vip", "site", "online",
    "shop", "blog", "news", "tech", "cloud", "cc", "hk", "tw", "jp", "kr",
    "de", "fr", "uk", "ru", "sg", "us", "ca", "au", "pro", "work", "ltd",
    "group", "email", "link", "live", "fun", "space", "website", "pub",
}

_CODE_KEYWORDS = (
    "def ", "class ", "function ", "return ", "import ", "from ", "export ",
    "const ", "let ", "var ", "elif ", "switch", "case ", "break;",
    "continue;", "print(", "println(", "console.", "public ", "private ",
    "protected ", "void ", "int ", "float ", "double ", "bool ",
    "new ", "delete ", "try {", "try:", "catch", "finally", "throw ",
    "#include", "#define", "package ", "namespace ", "using ", "lambda",
    "yield ", "async ", "await ", "self.", "this.", "nullptr",
    "if (", "if(", "for (", "for(", "while (", "while(",
    "=>", "->", "::", "&&", "||", "===", "!==",
    "+=", "-=", "*=", "/=", "<?php", "func ", "SELECT ", "INSERT ",
    "UPDATE ", "DELETE FROM", "WHERE ", "GROUP BY",
)

_COMMENT_RE = re.compile(r"(^|\n)\s*(//|/\*|\*/|\*\s|#\s|--\s|<!--)")
_SYMBOLS = "{}[]();=<>+-*/&|_@$%^~!?:,."


def _is_single_url(line):
    if _SCHEME_RE.match(line) or _MAILTO_RE.match(line) or _WWW_RE.match(line):
        return True
    m = _DOMAIN_RE.match(line)
    return bool(m) and m.group(1).lower() in _COMMON_TLDS


def is_link(text):
    t = (text or "").strip()
    if not t or len(t) > 4096:
        return False
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    if not lines or len(lines) > 20:
        return False
    for line in lines:
        if " " in line or "\t" in line:
            return False
        if not _is_single_url(line):
            return False
    return True


def is_code(text):
    s = text or ""
    if len(s.strip()) < 12:
        return False
    lines = s.splitlines()
    score = 0
    if s.count("{") >= 1 and s.count("}") >= 1:
        score += 2
    if s.count("(") >= 1 and s.count(")") >= 1:
        score += 1
    if s.count("[") >= 1 and s.count("]") >= 1:
        score += 1
    score += min(sum(1 for k in _CODE_KEYWORDS if k in s), 4)
    if any(l.rstrip().endswith((";", "{", "}")) for l in lines):
        score += 1
    if len(lines) >= 2 and any(
            l.startswith(("    ", "\t")) and l.strip() for l in lines):
        score += 1
    if _COMMENT_RE.search(s):
        score += 1
    symbols = sum(1 for c in s if c in _SYMBOLS)
    if symbols / len(s) > 0.10:
        score += 1
    # 中文占比高的段落大概率是普通文字，降权防误判
    cjk = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    if cjk / len(s) > 0.3:
        score -= 3
    return score >= 4


def classify_text(text):
    """返回 'link' / 'code' / 'text'。"""
    t = (text or "").strip()
    if not t:
        return "text"
    if is_link(t):
        return "link"
    if is_code(text or ""):
        return "code"
    return "text"
