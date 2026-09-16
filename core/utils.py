"""通用工具函数：token 估算、关键词提取、JSON 解析、时间处理。

本模块不依赖任何第三方库，可独立测试。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def now_ts() -> float:
    """返回当前 Unix 时间戳（秒，浮点）。"""
    return time.time()


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数。

    启发式规则：
    - CJK 字符（中日韩）按 1 字符 ≈ 1 token 计；
    - ASCII 连续串按 4 字符 ≈ 1 token 计。

    该估算不追求精确，仅用于注入上限与压缩阈值的近似控制。
    """
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    remaining = len(text) - cjk_count
    ascii_tokens = -(-remaining // 4) if remaining > 0 else 0  # ceil
    return cjk_count + ascii_tokens


def extract_keywords(text: str) -> list[str]:
    """从文本中提取用于匹配的关键词集合。

    - ASCII 词：直接按单词提取，转小写；
    - 中文：提取单字与二元组（bigram），提高部分匹配召回率。

    Returns:
        去重后的关键词列表。
    """
    if not text:
        return []
    keywords: list[str] = []
    for m in _ASCII_WORD_RE.finditer(text):
        keywords.append(m.group(0).lower())
    cjk_chars = _CJK_RE.findall(text)
    for ch in cjk_chars:
        keywords.append(ch)
    for a, b in zip(cjk_chars, cjk_chars[1:]):
        keywords.append(a + b)
    # 去重且保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def safe_json_loads(text: str) -> Any | None:
    """从 LLM 输出中尽力解析 JSON。

    依次尝试：
    1. 直接 json.loads；
    2. 提取 ```json ...``` 代码块；
    3. 截取首个 { 或 [ 到最后一个 } 或 ] 的片段。

    Returns:
        解析结果；全部失败返回 None。
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    for m in _JSON_FENCE_RE.finditer(text):
        candidates.append(m.group(1).strip())
    first_obj = min(
        (i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1
    )
    if first_obj >= 0:
        last = max(text.rfind("}"), text.rfind("]"))
        if last > first_obj:
            candidates.append(text[first_obj : last + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def clamp(value: float, low: float, high: float) -> float:
    """将数值限制在 [low, high] 区间。"""
    return max(low, min(high, value))


def parse_tags(raw: Any) -> list[str]:
    """把任意输入规范化为标签列表（JSON 数组或字符串均可）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raw = [p for p in re.split(r"[,，;；\s]+", raw) if p]
    if not isinstance(raw, list):
        return []
    tags = [str(t).strip() for t in raw if str(t).strip()]
    return tags[:16]
