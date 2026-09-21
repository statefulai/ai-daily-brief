"""
AI News Aggregator - Two-Stage Curation Pipeline

Stage 1: Score + classify + cluster similar topics
Stage 2: Editor-in-chief curation (pick a focus and a variable number of supporting items)
"""

import os
import json
import logging
import re
from openai import OpenAI
from sources import NewsItem

logger = logging.getLogger(__name__)

OPAQUE_REFERENCE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:two|three|four|multiple|2|3|4)\s+"
    r"(?:\w+\s+){0,2}(?:settings|case studies|cases|methods)\b"
    r"|(?:两项|两个|多项).{0,12}(?:设置|案例|方法)"
    r")",
    re.IGNORECASE,
)
INTERNAL_TERM_PATTERN = re.compile(
    r"\bimportance\b|内部评分|候选编号",
    re.IGNORECASE,
)
ORGANIZATION_ALIASES = {
    "OpenAI": ("openai",),
    "Microsoft": ("microsoft",),
    "Anthropic": ("anthropic", "claude"),
    "Meta": ("meta", "zuckerberg"),
    "Google": ("google", "gemini", "deepmind"),
    "NVIDIA": ("nvidia",),
    "Apple": ("apple",),
    "Amazon": ("amazon", "aws"),
    "xAI": ("xai", "grok"),
}
VAGUE_IMPACT_PHRASES = (
    "赛道升温", "竞争公开化", "竞争进一步公开化", "资本分化",
    "值得关注", "释放信号", "敲响警钟", "预示未来",
)


class CurationError(RuntimeError):
    """Raised when the LLM cannot produce a publishable daily brief."""


# Stage 1: Score and classify
STAGE1_PROMPT = """You are an AI news analyst. Score and classify these items.

Respond in JSON:
{
  "items": [
    {
      "index": 0,
      "relevance": "core",
      "category": "tool",
      "importance": 8,
      "topic_key": "gpt-5.5-release"
    }
  ]
}

Rules:
- Return exactly one item for every input index, including off-topic items; never omit or duplicate an index
- relevance: "core" (directly about AI/ML/dev-tools) | "adjacent" (tech industry or developer-adjacent) | "off-topic" (unrelated to AI/developers)
- category: "product" | "tool" | "research" | "industry" | "tutorial"
- importance: 1-10 (impact on AI developers; off-topic items MUST receive importance ≤ 2, adjacent items typically 3-5)
- topic_key: short identifier for the topic (same key = same event across sources)"""

# Stage 2: Editor-in-chief curation
STAGE2_PROMPT = """你是一位面向开发者的 AI 技术日报主编。从候选新闻中策展今日简报。

要求：
1. 选 1 条作为"今日焦点"，提供 18 字以内的中文短标题；编辑评论严格写成"要点：……；影响：……"，只写候选信息能支持的内容
2. 按当天实际价值选择 0 条或多条"热点速览"，不设固定篇数；每条提供 18 字以内的中文短标题，点评严格写成"要点：……；影响：……"，控制在 50 字以内
3. 选 0-2 个作为"今日工具"（优先开源项目，不要和焦点/速览重复）。只有能从来源或标题摘要说明"为什么今天入选"时才选择；理由严格写成"入选依据：来自今日实际来源名，……；用途：……"，不能只写常规简介
选稿标准：
- 重大模型发布/技术突破 > 工具更新 > 行业分析
- 全球影响力大的事件优先作为焦点
- 避免同一事件重复占位
- 今日焦点与热点速览合计，同一主要公司最多 2 条
- 每条入选内容必须不点链接也能理解：写清具体对象、动作或结果及其影响
- "要点"直接写具体对象、动作或结果；来源已在标题旁展示，不重复来源名，也不要复述英文文章标题
- "影响"必须落到开发者、团队或用户的工具选择、工作流、成本、安全、部署、采购或近期动作；不要只写赛道升温、竞争公开化、资本分化
- 禁止用"两个设置、两项案例、该研究、这一方法"等指代词代替关键信息；候选信息不足以说明具体内容时不要入选，也不要补写猜测
- 工具区不要选已经出现在焦点或速览中的条目
- 面向普通中文读者，不默认读者了解英文缩写或专业术语；无法避免时用短语解释
- 生僻缩写或英文术语首次出现时，用 4-8 个中文字补充含义
- 中文和英文或数字相邻时留一个空格，例如"评估 GPT-5.6 成本"
- 面向读者的标题、点评和推荐理由不得出现 importance、内部评分或候选编号
- 避免"值得关注、释放信号、敲响警钟、预示未来"等空泛套话，优先写可验证的变化、影响对象和行动含义

Respond in JSON:
{
  "focus": {
    "index": 0,
    "title_zh": "中文短标题",
    "editorial": "要点：……；影响：……"
  },
  "highlights": [
    {
      "index": 1,
      "title_zh": "中文短标题",
      "editorial": "要点：……；影响：……"
    }
  ],
  "tools": [
    {
      "index": 5,
      "title_zh": "中文短标题",
      "reason": "入选依据：来自今日 GitHub Trending；用途：适合……"
    }
  ]
}"""


def create_client(config: dict) -> OpenAI:
    """Create an OpenAI-compatible client from environment or project config."""
    options = {
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "timeout": 120.0,
    }
    base_url = os.environ.get("OPENAI_BASE_URL") or config.get("base_url")
    if base_url:
        options["base_url"] = base_url
    return OpenAI(**options)


def _run_stage1(items: list[NewsItem], config: dict) -> list[dict]:
    """Stage 1: Score, classify, and assign topic keys."""
    client = create_client(config)
    model = os.environ.get("AI_NEWS_MODEL", config.get("model", "gpt-5.4"))
    scored = []
    batch_size = 20

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        batch_text = "\n".join(
            f"[{j}] {item.title} | source={item.source} | score={item.score} | "
            f"url={item.url}"
            for j, item in enumerate(batch)
        )

        try:
            messages = [
                {"role": "system", "content": STAGE1_PROMPT},
                {"role": "user", "content": batch_text},
            ]
            # Repair one incomplete response, then remain fail-closed.
            for attempt in range(2):
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=config.get("max_tokens", 1024),
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )

                result = json.loads(response.choices[0].message.content)
                entries = result.get("items")
                expected_indices = set(range(len(batch)))
                received_indices = (
                    [
                        entry.get("index")
                        for entry in entries
                        if isinstance(entry, dict)
                    ]
                    if isinstance(entries, list)
                    else []
                )
                received_index_set = {
                    index for index in received_indices if type(index) is int
                }
                if (
                    isinstance(entries, list)
                    and len(entries) == len(batch)
                    and received_index_set == expected_indices
                ):
                    break

                error = CurationError(
                    f"Stage1 batch {i//batch_size + 1} returned incomplete scoring "
                    f"(expected indices {sorted(expected_indices)}, "
                    f"received {received_indices})"
                )
                if attempt == 0:
                    logger.warning(f"Stage1 retrying after validation: {error}")
                    messages.append({
                        "role": "user",
                        "content": (
                            f"The previous response was incomplete. Return exactly "
                            f"{len(batch)} items, one for each index from 0 to "
                            f"{len(batch) - 1}. Do not omit off-topic items."
                        ),
                    })
                    continue
                raise error

            for entry in entries:
                idx = entry.get("index", 0)
                importance = entry.get("importance", 0)
                if (
                    type(idx) is not int
                    or type(importance) is not int
                    or not 1 <= importance <= 10
                    or entry.get("category") not in {
                        "product", "tool", "research", "industry", "tutorial"
                    }
                    or not isinstance(entry.get("topic_key"), str)
                    or not entry["topic_key"].strip()
                ):
                    raise CurationError(
                        f"Stage1 batch {i//batch_size + 1} returned invalid scoring"
                    )

                if idx < len(batch) and importance >= 3:
                    item = batch[idx]
                    scored.append({
                        "title": item.title,
                        "url": item.url,
                        "source": item.source,
                        "score": item.score,
                        "published": item.published.isoformat(),
                        "summary": (item.summary or "")[:200],
                        "category": entry.get("category", "tool"),
                        "importance": importance,
                        "topic_key": entry.get("topic_key", f"item-{i+idx}"),
                        "tags": item.tags,
                    })

            logger.info(f"Stage1 batch {i//batch_size + 1}: "
                        f"{len(batch)} → {sum(1 for e in entries if e.get('importance', 0) >= 3)} scored (importance≥3)")

        except CurationError:
            raise
        except Exception as e:
            raise CurationError(
                f"Stage1 batch {i//batch_size + 1} failed: {e}"
            ) from e

    scored.sort(key=lambda x: (-x["importance"], -x["score"]))
    return scored


def _cluster_and_select_candidates(scored: list[dict], max_candidates: int = 20) -> list[dict]:
    """Cluster by topic_key, keep best per topic, return top candidates."""
    topic_best: dict[str, dict] = {}
    topic_all: dict[str, list[dict]] = {}

    for item in scored:
        key = item["topic_key"]
        topic_all.setdefault(key, []).append(item)
        if key not in topic_best or item["importance"] > topic_best[key]["importance"]:
            topic_best[key] = item

    # Attach related sources to each best item
    candidates = []
    for key, best in topic_best.items():
        related = [it for it in topic_all[key] if it["url"] != best["url"]]
        best["related_sources"] = [
            {"title": r["title"], "url": r["url"], "source": r["source"]}
            for r in related[:3]
        ]
        candidates.append(best)

    candidates.sort(key=lambda x: (-x["importance"], -x["score"]))

    # Enforce source diversity: max 5 items per source in candidates
    source_count: dict[str, int] = {}
    diverse = []
    for item in candidates:
        src = item["source"]
        source_count[src] = source_count.get(src, 0) + 1
        if source_count[src] <= 5:
            diverse.append(item)
        if len(diverse) >= max_candidates:
            break

    logger.info(f"Clustering: {len(scored)} scored → {len(topic_best)} topics → {len(diverse)} candidates")
    return diverse


def _run_stage2(candidates: list[dict], config: dict) -> dict:
    """Stage 2: Editor-in-chief curation."""
    if not candidates:
        raise CurationError("Stage1 produced no publishable candidates")

    client = create_client(config)
    model = os.environ.get("AI_NEWS_MODEL", config.get("model", "gpt-5.4"))

    eligible_candidates = [
        (i, item)
        for i, item in enumerate(candidates)
        if not OPAQUE_REFERENCE_PATTERN.search(item.get("title", ""))
    ]
    if not eligible_candidates:
        raise CurationError(
            "Stage2 has no self-contained candidate for publication"
        )

    candidate_text = "\n".join(
        f"[{i}] {item['title']} | source={item['source']} | "
        f"category={item['category']} | "
        f"related={len(item.get('related_sources', []))} sources"
        + (f"\n    摘要: {item['summary']}" if item.get("summary") else "")
        for i, item in eligible_candidates
    )

    base_messages = [
        {"role": "system", "content": STAGE2_PROMPT},
        {
            "role": "user",
            "content": (
                f"今日候选新闻（{len(eligible_candidates)} 条）:\n\n"
                f"{candidate_text}"
            ),
        },
    ]
    validation_error = None

    try:
        # Use editorial model (higher quality) for Stage 2.
        # Retry once only when the JSON is valid but fails publication rules.
        editorial_model = config.get("model_editorial", model)
        for attempt in range(2):
            messages = list(base_messages)
            if validation_error:
                messages.append({
                    "role": "user",
                    "content": (
                        f"上次选稿不符合发布规则：{validation_error}。"
                        "请重新选择并输出完整 JSON。"
                    ),
                })
            response = client.chat.completions.create(
                model=editorial_model,
                messages=messages,
                max_tokens=4096,
                temperature=0.4,
                response_format={"type": "json_object"},
            )

            brief = json.loads(response.choices[0].message.content)
            try:
                _validate_brief(brief, candidates)
            except CurationError as error:
                if attempt == 0:
                    validation_error = str(error)
                    logger.warning(f"Stage2 retrying after validation: {error}")
                    continue
                raise

            logger.info(f"Stage2: focus={brief.get('focus', {}).get('index')}, "
                        f"highlights={len(brief.get('highlights', []))}, "
                        f"tools={len(brief.get('tools', []))}")
            return brief

    except CurationError:
        raise
    except Exception as e:
        raise CurationError(f"Stage2 curation failed: {e}") from e


def _validate_brief(brief: dict, candidates: list[dict]):
    """Reject incomplete or non-Chinese editorial output before publication."""
    if not isinstance(brief, dict):
        raise CurationError("Stage2 returned an invalid brief")

    candidate_count = len(candidates)

    def valid_index(value) -> bool:
        return type(value) is int and 0 <= value < candidate_count

    def readable_chinese(value) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and any("\u4e00" <= char <= "\u9fff" for char in value)
        )

    def readable_title(value) -> bool:
        return (
            readable_chinese(value)
            and len(value.strip()) <= 30
            and not INTERNAL_TERM_PATTERN.search(value)
        )

    opaque_phrases = (
        "两个设置", "两项设置", "两个案例", "两项案例",
        "该研究", "这项研究", "这一方法", "该方法",
        "这条进展", "这项变化", "该事件",
    )

    def concrete_editorial(value) -> bool:
        return (
            readable_chinese(value)
            and not OPAQUE_REFERENCE_PATTERN.search(value)
            and not INTERNAL_TERM_PATTERN.search(value)
            and not any(phrase in value for phrase in opaque_phrases)
        )

    def has_concrete_impact(value) -> bool:
        if not isinstance(value, str):
            return False
        normalized = value.replace("影响:", "影响：")
        if "影响：" not in normalized:
            return False
        impact = normalized.split("影响：", 1)[1].strip()
        # Reject known vague prose instead of requiring a brittle keyword allowlist.
        has_chinese = any("\u4e00" <= char <= "\u9fff" for char in impact)
        meaningful_length = sum(char.isalnum() for char in impact)
        return (
            has_chinese
            and meaningful_length >= 6
            and not any(phrase in impact for phrase in VAGUE_IMPACT_PHRASES)
        )

    def primary_organization(index: int):
        title = candidates[index].get("title", "").lower()
        matches = []
        for organization, aliases in ORGANIZATION_ALIASES.items():
            positions = [title.find(alias) for alias in aliases if alias in title]
            if positions:
                matches.append((min(positions), organization))
        return min(matches)[1] if matches else None

    focus = brief.get("focus")
    if (
        not isinstance(focus, dict)
        or not valid_index(focus.get("index"))
        or not readable_title(focus.get("title_zh"))
        or not concrete_editorial(focus.get("editorial"))
    ):
        raise CurationError("Stage2 returned an unusable focus editorial")
    if not focus["editorial"].strip().startswith("要点："):
        raise CurationError("focus lacks the required point label")
    if not has_concrete_impact(focus["editorial"]):
        raise CurationError("focus lacks a concrete reader impact")
    highlights = brief.get("highlights")
    if not isinstance(highlights, list) or len(highlights) > max(candidate_count - 1, 0):
        raise CurationError("Stage2 returned an invalid highlight selection")

    selected_indices = {focus["index"]}
    for highlight in highlights:
        if (
            not isinstance(highlight, dict)
            or not valid_index(highlight.get("index"))
            or highlight["index"] in selected_indices
            or not readable_title(highlight.get("title_zh"))
            or not concrete_editorial(highlight.get("editorial"))
        ):
            raise CurationError("Stage2 returned an unusable highlight editorial")
        if not highlight["editorial"].strip().startswith("要点："):
            raise CurationError("highlight lacks the required point label")
        if not has_concrete_impact(highlight["editorial"]):
            raise CurationError(
                f"highlight {highlight['index']} lacks a concrete reader impact"
            )
        selected_indices.add(highlight["index"])

    organization_counts = {}
    for index in selected_indices:
        organization = primary_organization(index)
        if not organization:
            continue
        organization_counts[organization] = organization_counts.get(organization, 0) + 1
        if organization_counts[organization] > 2:
            raise CurationError(
                f"more than two editorial items about {organization}"
            )

    tools = brief.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 2:
        raise CurationError("Stage2 returned an invalid tool selection")
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or not valid_index(tool.get("index"))
            or tool["index"] in selected_indices
            or candidates[tool["index"]].get("category") != "tool"
            or not readable_title(tool.get("title_zh"))
            or not readable_chinese(tool.get("reason"))
            or INTERNAL_TERM_PATTERN.search(tool["reason"])
            or "入选依据" not in tool["reason"]
            or "用途" not in tool["reason"]
            or candidates[tool["index"]].get("source") not in tool["reason"]
        ):
            raise CurationError("Stage2 returned an unusable tool recommendation")
        selected_indices.add(tool["index"])


def curate_daily_brief(items: list[NewsItem], config: dict, *, jev: dict | None = None) -> dict:
    """
    Full two-stage curation pipeline.

    Jev assist runs after same-topic clustering and before editorial selection.
    Scores are annotations only; Stage 2 still sees every clustered candidate.
    """
    from generation.jev.assist import assist_candidates

    logger.info("=== Stage 1: Score & Classify ===")
    scored = _run_stage1(items, config)

    logger.info("=== Clustering & Candidate Selection ===")
    candidates = _cluster_and_select_candidates(scored)

    if not candidates:
        return {"candidates": [], "brief": {}}

    logger.info("=== Jev assist (advisory only) ===")
    assist = assist_candidates(candidates, config=jev or {})
    candidates = assist.candidates

    logger.info("=== Stage 2: Editorial Curation ===")
    brief = _run_stage2(candidates, config)

    result = {"candidates": candidates, "brief": brief}
    if assist.summary:
        result["jev_assist"] = assist.summary
    return result
