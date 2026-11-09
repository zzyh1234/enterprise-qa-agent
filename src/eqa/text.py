"""轻量文本处理：归一化、切词（中文单字 + 二元组 + 英文/数字词）、句子切分。

为什么不用第三方分词器：项目要求离线可运行、零安装依赖；中文制度文本里
「报销时限 / 年休假 / 重新认证」这类词以二元组即可稳定召回，配合同义词改写
（data/query_expansions.json）足以支撑演示与测试。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Set

ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._%+-]*")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
SENTENCE_RE = re.compile(r"[^。；!?！？\n]+[。；!?！？]?")

# 仅在「二次改写检索」时剔除的口语/疑问成分，避免污染检索词。
STOPWORDS = {
    "请问",
    "一下",
    "吗",
    "呢",
    "吧",
    "啊",
    "我",
    "我们",
    "你",
    "您",
    "的",
    "了",
    "是",
    "有",
    "和",
    "与",
    "以及",
    "还能",
    "可以",
    "怎么",
    "怎么办",
    "能不能",
    "是否",
    "这个",
    "那个",
    "它",
    "就是",
    "想问",
}


def normalize(text: str) -> str:
    """全角转半角、转小写、压缩空白。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> List[str]:
    """返回检索用 token：英文/数字词 + 中文单字 + 中文二元组。"""
    norm = normalize(text)
    tokens: List[str] = ASCII_TOKEN_RE.findall(norm)
    for run in CJK_RUN_RE.findall(norm):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def bigrams(text: str) -> List[str]:
    """只保留中文二元组与英文词，用于计算查询覆盖率。"""
    norm = normalize(text)
    terms: List[str] = ASCII_TOKEN_RE.findall(norm)
    for run in CJK_RUN_RE.findall(norm):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    return terms


def coverage(query_terms: Iterable[str], text_tokens: Set[str]) -> float:
    """查询词覆盖率：多少比例的查询词真的出现在文本里。"""
    terms = [t for t in query_terms if t]
    if not terms:
        return 0.0
    hit = sum(1 for t in terms if t in text_tokens)
    return hit / len(terms)


def split_sentences(text: str) -> List[str]:
    return [s.strip() for s in SENTENCE_RE.findall(text) if s.strip()]


def sentence_with(text: str, needle: str) -> str:
    """返回包含 needle 的最小完整句子；找不到时返回整段。"""
    if needle:
        for sentence in split_sentences(text):
            if needle in sentence:
                return sentence
    return text.strip()


def strip_stopwords(text: str) -> str:
    result = normalize(text)
    for word in sorted(STOPWORDS, key=len, reverse=True):
        result = result.replace(word, " ")
    return re.sub(r"\s+", " ", result).strip()
