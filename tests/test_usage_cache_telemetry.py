"""F19: prompt-cache hit telemetry must survive the model -> fact pipeline.

Long-task input cost is dominated by prompt-cache hit rate; the usage parser
previously dropped both provider shapes (OpenAI ``prompt_tokens_details.
cached_tokens`` and DeepSeek-native ``prompt_cache_hit_tokens``), so cache
performance was unobservable from durable facts.
"""
from __future__ import annotations

import unittest
from uuid import uuid4

from koawa_agent_v2.model.openai_client import _parse_usage
from koawa_agent_v2.model.protocol import ModelTurn, ModelUsage
from koawa_agent_v2.recovery.execution import model_turn_document


class UsageCacheTelemetryTest(unittest.TestCase):
    def test_openai_details_shape_is_parsed(self) -> None:
        usage = _parse_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "total_tokens": 1050,
                "prompt_tokens_details": {"cached_tokens": 880},
            }
        )
        self.assertEqual(880, usage.cached_input_tokens)

    def test_deepseek_native_shape_is_parsed(self) -> None:
        usage = _parse_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "prompt_cache_hit_tokens": 940,
            }
        )
        self.assertEqual(940, usage.cached_input_tokens)

    def test_unreported_cache_stays_none(self) -> None:
        usage = _parse_usage({"prompt_tokens": 10, "completion_tokens": 2})
        self.assertIsNone(usage.cached_input_tokens)
        self.assertIsNone(usage.total_tokens)

    def test_openai_shape_wins_over_native_fallback(self) -> None:
        usage = _parse_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 70},
                "prompt_cache_hit_tokens": 30,
            }
        )
        self.assertEqual(70, usage.cached_input_tokens)

    def test_negative_cache_is_rejected(self) -> None:
        from koawa_agent_v2.model.openai_client import _AdapterFault

        with self.assertRaises(_AdapterFault) as raised:
            _parse_usage(
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": -1},
                }
            )
        self.assertEqual("openai.invalid_usage", raised.exception.code)

    def test_model_turn_document_carries_cached_tokens(self) -> None:
        from koawa_agent_v2.model.protocol import FinishReason

        turn = ModelTurn(
            model_turn_id=uuid4(),
            provider="test",
            model="m",
            provider_response_id="r",
            output_items=(),
            finish_reason=FinishReason.STOP,
            usage=ModelUsage(100, 5, 105, 90),
        )
        document = model_turn_document(turn)
        self.assertEqual(
            {
                "input_tokens": 100,
                "output_tokens": 5,
                "total_tokens": 105,
                "cached_input_tokens": 90,
            },
            document["usage"],
        )


if __name__ == "__main__":
    unittest.main()
