"""在同一组原文短片段上进行 BM25 排序、RRF 融合和去重。"""

import math
import re
from collections import Counter
from difflib import SequenceMatcher

PASSAGE_CHARS = 400
PASSAGE_OVERLAP = 80
CANDIDATE_LIMIT = 20
RRF_K = 60


def passages(chunks: list[dict]) -> list[dict]:
    result = []
    for chunk in chunks:
        text = chunk["content"]
        for start in range(0, len(text), PASSAGE_CHARS - PASSAGE_OVERLAP):
            content = text[start:start + PASSAGE_CHARS]
            if content.strip():
                result.append({**chunk, "content": content, "char_start": start})
            if start + PASSAGE_CHARS >= len(text):
                break
    return result


def _tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    for sequence in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(sequence[index:index + 2] for index in range(len(sequence) - 1))
        if len(sequence) == 1:
            tokens.append(sequence)
    return tokens


def rank_bm25(query: str, items: list[dict], limit: int = CANDIDATE_LIMIT) -> list[dict]:
    """标准 BM25：词频饱和、逆文档频率和长度归一化。零匹配不回退到开头。"""
    terms = set(_tokens(query))
    if not items or not terms:
        return []
    counts = [Counter(_tokens(item["content"])) for item in items]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / len(lengths) or 1
    frequency = Counter(term for count in counts for term in terms if term in count)
    k1, b = 1.5, 0.75
    scored = []
    for index, (item, count, length) in enumerate(zip(items, counts, lengths)):
        score = 0.0
        for term in terms & count.keys():
            idf = math.log(1 + (len(items) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            tf = count[term]
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / average))
        if score > 0:
            scored.append((score, index, item))
    return [{**item, "score": round(score, 6)}
            for score, _, item in sorted(scored, key=lambda row: (-row[0], row[1]))[:limit]]


def _key(item: dict) -> tuple:
    return item["file_id"], item["chunk_index"], item.get("char_start", 0)


def fuse_rankings(*rankings: list[dict]) -> list[dict]:
    """RRF 使用排名而非相加量纲不同的分数；同一路重复项只计一次。"""
    scores, items = {}, {}
    for ranking in rankings:
        seen = set()
        rank = 0
        for item in ranking:
            key = _key(item)
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            items.setdefault(key, item)
            scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank)
    ordered = sorted(items, key=lambda key: -scores[key])
    return [{**items[key], "score": round(scores[key], 6)} for key in ordered]


def select_distinct(items: list[dict], limit: int = 4) -> list[dict]:
    """每块仅保留最相关窗口，抑制同文件相邻分块的近乎重复正文。"""
    result, chunks, texts = [], set(), set()
    for item in items:
        chunk_key = item["file_id"], item["chunk_index"]
        text = re.sub(r"\s+", " ", item["content"]).strip()
        text_key = item["file_id"], text
        if chunk_key in chunks or text_key in texts:
            continue
        duplicate = False
        for previous in result:
            if (previous["file_id"], previous["location"]) != (item["file_id"], item["location"]):
                continue
            other = re.sub(r"\s+", " ", previous["content"]).strip()
            match = SequenceMatcher(None, text, other, autojunk=False).find_longest_match()
            if match.size >= 120 and match.size >= 0.8 * min(len(text), len(other)):
                duplicate = True
                break
        if duplicate:
            continue
        result.append(item)
        chunks.add(chunk_key)
        texts.add(text_key)
        if len(result) >= limit:
            break
    return result
