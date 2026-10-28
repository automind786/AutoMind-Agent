import os
import unittest
from unittest.mock import patch
import ai_hub
import agent_board_ui

class SilentProviderTests(unittest.TestCase):
    def test_no_keys_returns_helpful_error(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(ai_hub.config, 'GEMINI_API_KEY', '', create=True), \
             patch.object(ai_hub.config, 'GOOGLE_API_KEY', '', create=True), \
             patch.object(ai_hub.config, 'ANTHROPIC_API_KEY', '', create=True), \
             patch.object(ai_hub.config, 'CLAUDE_API_KEY', '', create=True), \
             patch.object(ai_hub.config, 'GROQ_API_KEY', '', create=True), \
             patch.object(ai_hub.config, 'OPENROUTER_API_KEY', '', create=True):
            result = ai_hub.complete_silent('hello')
        self.assertFalse(result['ok'])
        self.assertIn('No AI API keys', result['error'])

    def test_fallback_to_second_configured_provider(self):
        with patch.object(ai_hub, '_provider_order', return_value=['gemini', 'groq']), \
             patch.object(ai_hub, '_call_provider', side_effect=[RuntimeError('rate limit'), 'works']):
            result = ai_hub.complete_silent('hello')
        self.assertTrue(result['ok'])
        self.assertEqual(result['provider'], 'groq')
        self.assertEqual(len(result['attempts']), 2)


    def test_provider_error_redacts_api_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"}, clear=True), \
             patch.object(ai_hub, "_provider_order", return_value=["gemini"]), \
             patch.object(ai_hub, "_call_provider", side_effect=RuntimeError("request https://x/?key=secret-test-key failed")):
            result = ai_hub.complete_silent("hello")
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-test-key", str(result))
        self.assertIn("[REDACTED]", str(result))

    def test_board_specced_entrypoints_resolve(self):
        backend = agent_board_ui.Backend.__new__(agent_board_ui.Backend)
        backend._mods = {}
        backend._mod_err = {}
        backend._inst = {}
        backend.overrides = {}
        for kind in ('plan', 'codegen', 'evolve', 'math', 'status'):
            label, fn = backend.find(kind)
            self.assertIsNotNone(fn, kind)
            self.assertTrue(callable(fn), kind)

    def test_board_plan_is_preview_only(self):
        import main
        result = main.board_run_plan('make a calculator app')
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'planned_only')
        self.assertTrue(result['execution_requires_explicit_approval'])

if __name__ == '__main__':
    unittest.main()
