import unittest
from unittest.mock import patch

from scene_pilot.core.config import Settings
from scene_pilot.core.llm import create_qwen_model


class ModelConfigurationTests(unittest.TestCase):
    def test_model_retries_output_and_usage(self):
        settings = Settings(
            "offline-key", "offline-model", "https://example.invalid", ""
        )
        with patch("scene_pilot.core.llm.ChatOpenAI") as constructor:
            create_qwen_model(settings)
            kwargs = constructor.call_args.kwargs
            self.assertEqual(kwargs["max_retries"], 0)
            self.assertEqual(kwargs["max_tokens"], 1600)
            self.assertTrue(kwargs["stream_usage"])
