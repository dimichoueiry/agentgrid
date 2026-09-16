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

    def test_legacy_prompt_records_load_as_global(self):
        self.path.write_text(json.dumps([{"name": "old", "body": "Legacy"}], indent=2))
        prompt = web.load_saved_prompts("engineering")[0]
        self.assertEqual(prompt["areaId"], "")


if __name__ == "__main__":
    unittest.main()
