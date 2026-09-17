import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sources import NewsItem
from summarizer import (
    CurationError,
    _run_stage1,
    _run_stage2,
    create_client,
)


def completion(payload: dict):
    message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def candidate(index: int) -> dict:
    return {
        "title": f"News {index}",
        "url": f"https://example.com/{index}",
        "source": "Test",
        "score": 10 - index,
        "published": datetime.now(timezone.utc).isoformat(),
        "summary": "摘要",
        "category": "research",
        "importance": 8,
        "topic_key": f"topic-{index}",
        "tags": [],
        "related_sources": [],
    }


class SummarizerTest(unittest.TestCase):
    def test_create_client_prefers_environment_base_url(self):
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_BASE_URL": "https://env.example/v1",
                },
            ),
            patch("summarizer.OpenAI") as openai,
        ):
            create_client({"base_url": "https://config.example/v1"})

        openai.assert_called_once_with(
            api_key="test-key",
            base_url="https://env.example/v1",
            timeout=120.0,
        )

    def test_create_client_uses_sdk_default_without_base_url(self):
        with (
            patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "test-key"},
                clear=True,
            ),
            patch("summarizer.OpenAI") as openai,
        ):
            create_client({})

        openai.assert_called_once_with(api_key="test-key", timeout=120.0)

    def test_stage1_raises_instead_of_publishing_fallback_data(self):
        item = NewsItem(
            title="Test news",
            url="https://example.com/news",
            source="Test",
        )
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("rate limited")

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "rate limited"),
        ):
            _run_stage1([item], {})

    def test_stage1_retries_incomplete_scoring(self):
        items = [
            NewsItem(title=f"News {index}", url=f"https://example.com/{index}", source="Test")
            for index in range(2)
        ]
        client = MagicMock()
        incomplete = completion({
            "items": [{
                "index": 0,
                "category": "research",
                "importance": 8,
                "topic_key": "topic-0",
            }]
        })
        complete = completion({
            "items": [
                {
                    "index": index,
                    "category": "research",
                    "importance": 8,
                    "topic_key": f"topic-{index}",
                }
                for index in range(2)
            ]
        })
        client.chat.completions.create.side_effect = [incomplete, complete]

        with patch("summarizer.create_client", return_value=client):
            result = _run_stage1(items, {})

        self.assertEqual(len(result), 2)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        retry_prompt = client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
        self.assertIn("Return exactly 2 items", retry_prompt)

    def test_stage1_rejects_incomplete_scoring_after_retry(self):
        items = [
            NewsItem(title=f"News {index}", url=f"https://example.com/{index}", source="Test")
            for index in range(2)
        ]
        client = MagicMock()
        client.chat.completions.create.return_value = completion({
            "items": [{
                "index": 0,
                "category": "research",
                "importance": 8,
                "topic_key": "topic-0",
            }]
        })

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "expected indices.*received"),
        ):
            _run_stage1(items, {})

        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_stage2_rejects_empty_editorial(self):
        candidates = [candidate(index) for index in range(6)]
        client = MagicMock()
        client.chat.completions.create.return_value = completion({
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": "",
                }
                for index in range(1, 6)
            ],
            "tools": [],
        })

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "highlight editorial"),
        ):
            _run_stage2(candidates, {})

    def test_stage2_accepts_publishable_editorials(self):
        candidates = [candidate(index) for index in range(7)]
        candidates[6]["category"] = "tool"
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "模型选择发生变化",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可重新评估生产选型。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"开发进展{index}",
                    "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少线上等待时间。",
                }
                for index in range(1, 6)
            ],
            "tools": [
                {
                    "index": 6,
                    "title_zh": "本地部署工具",
                    "reason": "入选依据：来自今日 Test 候选，摘要明确给出新版本；用途：适合需要本地部署能力的团队试用。",
                }
            ],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with patch("summarizer.create_client", return_value=client):
            result = _run_stage2(candidates, {})

        self.assertEqual(result, brief)

    def test_stage2_accepts_variable_highlight_count(self):
        candidates = [candidate(index) for index in range(8)]
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "模型选择发生变化",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可重新评估生产选型。",
            },
            "highlights": [
                {
                    "index": 1,
                    "title_zh": "只留一条速览",
                    "editorial": "要点：工具更新了权限控制；影响：团队可减少人工配置步骤。",
                }
            ],
            "tools": [],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with patch("summarizer.create_client", return_value=client):
            result = _run_stage2(candidates, {})

        self.assertEqual(len(result["highlights"]), 1)

    def test_stage2_rejects_selection_without_chinese_title(self):
        candidates = [candidate(index) for index in range(6)]
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "English only",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少部署成本。",
                }
                for index in range(1, 6)
            ],
            "tools": [],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "focus editorial"),
        ):
            _run_stage2(candidates, {})

    def test_stage2_rejects_generic_tool_reason(self):
        candidates = [candidate(index) for index in range(7)]
        candidates[6]["category"] = "tool"
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少部署成本。",
                }
                for index in range(1, 6)
            ],
            "tools": [
                {
                    "index": 6,
                    "title_zh": "开源工具",
                    "reason": "适合开发团队试用。",
                }
            ],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "tool recommendation"),
        ):
            _run_stage2(candidates, {})

    def test_stage2_rejects_opaque_highlight_explanation(self):
        candidates = [candidate(index) for index in range(6)]
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": (
                        "两项 API 设置让分数提升了。"
                        if index == 1
                        else "要点：新模型降低了推理延迟；影响：开发团队可减少部署成本。"
                    ),
                }
                for index in range(1, 6)
            ],
            "tools": [],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "highlight editorial"),
        ):
            _run_stage2(candidates, {})

    def test_stage2_excludes_opaque_candidate_titles(self):
        candidates = [candidate(index) for index in range(7)]
        candidates[1]["title"] = "How two API settings tripled benchmark scores"
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少部署成本。",
                }
                for index in range(2, 7)
            ],
            "tools": [],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with patch("summarizer.create_client", return_value=client):
            _run_stage2(candidates, {})

        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertNotIn("How two API settings", prompt)
        self.assertNotIn("importance=", prompt)
        self.assertIn("[2] News 2", prompt)

    def test_stage2_rejects_internal_importance_in_tool_reason(self):
        candidates = [candidate(index) for index in range(7)]
        candidates[6]["category"] = "tool"
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少部署成本。",
                }
                for index in range(1, 6)
            ],
            "tools": [
                {
                    "index": 6,
                    "title_zh": "开源工具",
                    "reason": "入选依据：来自今日 Test，importance 为 8；用途：统一管理代码代理。",
                }
            ],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "tool recommendation"),
        ):
            _run_stage2(candidates, {})

    def test_stage2_retries_company_concentrated_selection(self):
        candidates = [candidate(index) for index in range(7)]
        candidates[0]["title"] = "Microsoft model launch"
        candidates[1]["title"] = "Microsoft Copilot update"
        candidates[2]["title"] = "Microsoft investment results"
        candidates[3]["title"] = "Google model update"
        candidates[4]["title"] = "Meta agent launch"
        candidates[5]["title"] = "OpenAI research program"
        candidates[6]["title"] = "Anthropic safety release"

        def brief_with_highlights(indices):
            return {
                "focus": {
                    "index": 0,
                    "title_zh": "微软模型发布",
                    "editorial": "要点：微软发布新模型；影响：开发团队可重新评估模型选择。",
                },
                "highlights": [
                    {
                        "index": index,
                        "title_zh": f"行业动态{index}",
                        "editorial": "要点：厂商发布新能力；影响：开发团队需评估工具选择。",
                    }
                    for index in indices
                ],
                "tools": [],
            }

        invalid_brief = brief_with_highlights([1, 2, 3, 4, 5])
        valid_brief = brief_with_highlights([1, 3, 4, 5, 6])
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            completion(invalid_brief),
            completion(valid_brief),
        ]

        with patch("summarizer.create_client", return_value=client):
            result = _run_stage2(candidates, {})

        self.assertEqual(result, valid_brief)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        retry_prompt = client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
        self.assertIn("more than two editorial items about Microsoft", retry_prompt)

    def test_stage2_rejects_vague_reader_impact(self):
        candidates = [candidate(index) for index in range(6)]
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": (
                        "要点：厂商发布新模型；影响：行业竞争进一步公开化。"
                        if index == 1
                        else "要点：新模型降低了延迟；影响：开发团队可减少部署成本。"
                    ),
                }
                for index in range(1, 6)
            ],
            "tools": [],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "concrete reader impact"),
        ):
            _run_stage2(candidates, {})

    def test_stage2_accepts_concrete_engineering_impact_with_ascii_colon(self):
        candidates = [candidate(index) for index in range(6)]
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": (
                        "要点：模型推理速度提升；影响: 工程师可缩短评测周期。"
                        if index == 1
                        else "要点：新模型降低了延迟；影响：开发团队可减少部署成本。"
                    ),
                }
                for index in range(1, 6)
            ],
            "tools": [],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with patch("summarizer.create_client", return_value=client):
            result = _run_stage2(candidates, {})

        self.assertEqual(result, brief)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_stage2_accepts_concrete_impact_without_keyword_allowlist(self):
        candidates = [candidate(index) for index in range(6)]
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可减少等待时间。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": (
                        "要点：模型推理速度提升；影响：可缩短评测周期并减少等待。"
                        if index == 1
                        else "要点：新模型降低了延迟；影响：开发团队可减少部署成本。"
                    ),
                }
                for index in range(1, 6)
            ],
            "tools": [],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with patch("summarizer.create_client", return_value=client):
            result = _run_stage2(candidates, {})

        self.assertEqual(result, brief)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_stage2_rejects_legacy_fact_label(self):
        candidates = [candidate(index) for index in range(6)]
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "焦点新闻",
                "editorial": "事实：新模型降低了延迟；影响：开发团队可减少部署成本。",
            },
            "highlights": [
                {
                    "index": index,
                    "title_zh": f"速览新闻{index}",
                    "editorial": "要点：新模型降低了延迟；影响：开发团队可减少部署成本。",
                }
                for index in range(1, 6)
            ],
            "tools": [],
        }
        client = MagicMock()
        client.chat.completions.create.return_value = completion(brief)

        with (
            patch("summarizer.create_client", return_value=client),
            self.assertRaisesRegex(CurationError, "required point label"),
        ):
            _run_stage2(candidates, {})

if __name__ == "__main__":
    unittest.main()
