"""JWT / JWS 解析（只读展示，不验签）。

设计边界：
- **绝不校验签名**：本地没有密钥，声称"验证通过"是误导。这里只解码结构，
  签名部分原样展示并明确标注"未校验"。想验签需要密钥，不属工具职责。
- **不信任 payload 内容**：即便格式完整，也只当数据展示，不据此放行任何操作。
- 容忍常见输入噪声：`Bearer ` 前缀、首尾引号、换行与首尾空白、URL-safe 的
  `-_` 与标准 base64 的 `+/` 混用、缺失的 `=` 填充。
"""

import base64
import binascii
import json
from dataclasses import dataclass, field


class JwtError(ValueError):
    """输入不是可解析的 JWT。"""


@dataclass(frozen=True)
class JwtResult:
    """解析结果。``header``/``payload`` 是格式化后的 JSON 文本。"""

    header_raw: str
    payload_raw: str
    signature: str
    header_json: str
    payload_json: str
    segments: int
    claims: dict = field(default_factory=dict)


# JWT 规范里常见的时间类声明（秒级 Unix 时间戳）。
_TIME_CLAIMS = ("exp", "iat", "nbf", "auth_time", "updated_at")


def strip_wrapping(text):
    """去掉 ``Bearer `` 前缀、成对引号与首尾空白。

    从浏览器 DevTools、日志、curl 命令里复制出来的 token 常带这些包裹，
    直接 b64 解码会因非法字符失败。
    """
    s = (text or "").strip()
    # 允许 "Bearer xxx" / "bearer xxx" / "Authorization: Bearer xxx"
    lower = s.lower()
    for prefix in ("authorization:", "bearer "):
        if lower.startswith(prefix):
            s = s[len(prefix):].strip()
            lower = s.lower()
    # 成对的单/双引号（JSON 字符串或代码字面量里复制出来的）
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    # 可能被换行折断，JWT 的 base64url 段内不会有空白
    return "".join(s.split())


def _b64url_decode(segment):
    """宽松解码：兼容 base64url 与标准 base64、缺失填充、首尾多余字符。"""
    s = segment.strip()
    if not s:
        raise JwtError("JWT 片段为空。")
    # 标准 base64 的 +/ 转回 URL-safe 的 -_，反向也接受
    s = s.replace("-", "+").replace("_", "/")
    # 补齐缺失的 = 填充：base64 长度必须是 4 的倍数
    padding = (-len(s)) % 4
    # 长度 4n+1 是非法 base64，补三个 = 也无解，提前给出可读错误
    if padding == 3:
        raise JwtError("JWT 片段长度非法，可能被截断。")
    s += "=" * padding
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise JwtError(f"JWT 片段不是合法的 base64url 编码：{exc}") from exc


def _decode_json_segment(segment, label):
    """把一段 base64url 解成格式化 JSON 文本，同时返回解析后的 dict。"""
    data = _b64url_decode(segment)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JwtError(f"{label} 不是 UTF-8 文本：{exc}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JwtError(f"{label} 不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 "
                       f"{exc.colno} 列）") from exc
    pretty = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False)
    # 原文同样保留一份：有人需要看未格式化的紧凑形态
    _ = text
    return pretty, obj


def format_epoch(value):
    """秒/毫秒时间戳 → 本地时间字符串；无法识别时返回空串。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    # 13 位是毫秒，10 位是秒；以 1e11 为界区分（约合公元 5138 年）
    if abs(number) >= 1e11:
        number /= 1000.0
    import datetime
    try:
        return datetime.datetime.fromtimestamp(number).strftime(
            "%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_jwt(token):
    """解析 JWT，返回 :class:`JwtResult`。签名为空串表示未带签名（JWS 未签名）。"""
    raw = strip_wrapping(token)
    if not raw:
        raise JwtError("请输入 JWT。")
    parts = raw.split(".")
    if len(parts) == 3:
        header_seg, payload_seg, signature = parts
    elif len(parts) == 2:
        # 未签名的 JWS（alg=none）常见于内部系统，只有 header.payload
        header_seg, payload_seg = parts
        signature = ""
    else:
        # 5 段是 JWE（加密 token），本工具不做解密
        if len(parts) == 5:
            raise JwtError("这是 JWE 加密令牌（5 段），本工具只能解析未加密的 JWS。")
        if len(parts) < 2:
            raise JwtError("不是 JWT：应以「.」分隔出 header.payload.signature 三段。")
        raise JwtError(f"JWT 段数异常（{len(parts)} 段），应为 2 段或 3 段。")

    header_json, header_obj = _decode_json_segment(header_seg, "Header")
    payload_json, payload_obj = _decode_json_segment(payload_seg, "Payload")
    if not isinstance(header_obj, dict):
        raise JwtError("Header 不是 JSON 对象。")
    return JwtResult(
        header_raw=header_seg,
        payload_raw=payload_seg,
        signature=signature,
        header_json=header_json,
        payload_json=payload_json,
        segments=len(parts),
        claims=payload_obj if isinstance(payload_obj, dict) else {},
    )


def summarize_claims(claims):
    """把时间类声明整理成可读行，供 UI 显示过期状态。"""
    lines = []
    for key in _TIME_CLAIMS:
        if key not in claims:
            continue
        human = format_epoch(claims[key])
        if human:
            lines.append(f"{key} = {human}")
    return lines


__all__ = [
    "JwtError", "JwtResult", "format_epoch", "parse_jwt",
    "strip_wrapping", "summarize_claims",
]
