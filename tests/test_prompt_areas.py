import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentgrid import web


class PromptAreaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "prompts.json"
        self.patch = patch.object(web, "PROMPTS_PATH", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_global_prompts_are_visible_in_every_area(self):
        web.save_saved_prompt("shared", "", "Shared", "")
        web.save_saved_prompt("marketing-plan", "", "Marketing", "marketing")
        self.assertEqual([p["name"] for p in web.load_saved_prompts("marketing")],
                         ["shared", "marketing-plan"])
        self.assertEqual([p["name"] for p in web.load_saved_prompts("engineering")],
                         ["shared"])

    def test_same_name_can_be_scoped_without_overwriting_global(self):
        web.save_saved_prompt("brief", "Global", "All", "")
        web.save_saved_prompt("brief", "Marketing", "Only marketing", "marketing")
        self.assertEqual(len(web.load_saved_prompts("*")), 2)
        self.assertEqual(web.load_saved_prompts("marketing")[1]["body"], "Only marketing")
        web.delete_saved_prompt("brief", "marketing")
        self.assertEqual(web.load_saved_prompts("*")[0]["body"], "All")

    def test_a_long_prompt_body_is_kept_whole(self):
        # A saved prompt's body is prose, and Insert drops it straight into a
        # system-prompt field, so it is never truncated. It used to be cut at
        # 20,000 characters, silently losing the tail of a long prompt.
        long_body = "Break the work into small, checkable steps. " * 3000
        web.save_saved_prompt("long-one", "A long prompt", long_body)
        (saved,) = [p for p in web.load_saved_prompts() if p["name"] == "long-one"]
        self.assertEqual(saved["body"], long_body.strip())
        self.assertGreater(len(saved["body"]), 100_000)

    def test_legacy_prompt_records_load_as_global(self):
        self.path.write_text(json.dumps([{"name": "old", "body": "Legacy"}], indent=2))
        prompt = web.load_saved_prompts("engineering")[0]
        self.assertEqual(prompt["areaId"], "")


if __name__ == "__main__":
    unittest.main()
