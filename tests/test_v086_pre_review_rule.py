"""The v0.8.5 bench, 18 Nov 2026: the pre-review had no rule of its own.

The supervisor saw only the answer schema. A Codex supervisor bounced 9 of 36
farm cards whose code passed the hidden tests: three for thin tests (which the
implementer may not change) and six for robustness the Done text did not ask
for. The pre-review now says what a bounce is for.
"""
import unittest

from openfarm_core.micromanager import _build_op_prompt


class PreReviewRuleTests(unittest.TestCase):
    def test_the_rule_reaches_the_supervisor(self) -> None:
        prompt = _build_op_prompt("pre_review", {"card": {"id": "C"}})
        rules = prompt.split("Rules: ", 1)[1].split("\n", 1)[0]
        self.assertIn("Done text", rules)
        self.assertIn("thin tests are never a reason to bounce", rules)
        self.assertIn("`missed_clause`", rules)
        self.assertLess(prompt.index("Rules: "), prompt.index("Required JSON schema"))


if __name__ == "__main__":
    unittest.main()
