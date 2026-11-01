"""contexttax on a small made-up log: long-context turns, a sub-agent, a re-read and a loop."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import contexttax  # noqa: E402


def call(i, read, written=0, out=100, side=False, model="claude-opus-5-5", t="2026-10-20T10:00:00Z", tools=()):
    content = [{"type": "tool_use", "id": f"tu{i}-{n}", "name": name, "input": inp} for n, (name, inp) in enumerate(tools)]
    return {
        "type": "assistant", "isSidechain": side, "timestamp": t,
        "message": {"id": f"m{i}", "model": model, "content": content,
                    "usage": {"input_tokens": 0, "cache_read_input_tokens": read, "cache_creation_input_tokens": written,
                              "cache_creation": {"ephemeral_1h_input_tokens": written, "ephemeral_5m_input_tokens": 0}, "output_tokens": out}},
    }


class ContextTax(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        read = {"file_path": "/a.py"}
        lines = [
            call(1, 50_000, 10_000, tools=[("Read", read)]),
            call(2, 60_000, tools=[("Read", read)]),
            call(3, 300_000, tools=[("Bash", {"command": "zzsecretzz", "description": "a"})]),
            call(4, 300_000, tools=[("Bash", {"command": "zzsecretzz", "description": "b"})]),
            call(5, 600_000, tools=[("Bash", {"command": "zzsecretzz", "description": "c"})]),
            call(6, 20_000, side=True),
            # The same message logged twice (one line per content block) counts once.
            call(6, 20_000, side=True),
        ]
        os.makedirs(os.path.join(self.dir, "proj"))
        with open(os.path.join(self.dir, "proj", "s.jsonl"), "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            f.write("not json\n")

    def test_numbers(self):
        r = contexttax.analyse(self.dir, None)
        self.assertEqual(r["sessions"], 1)
        self.assertEqual(r["turns"], 6)
        self.assertEqual(r["rereads"], (1, 2))
        self.assertEqual(r["loops"], 1)  # three identical shell calls; the description is ignored
        self.assertGreater(r["big_context_share"], 0.6)
        self.assertGreater(r["blamed"]["subagents"], 0)
        self.assertAlmostEqual(sum(r["parts"].values()), 1.0, places=6)

    def test_report_has_no_log_text(self):
        out = contexttax.report(contexttax.analyse(self.dir, None))
        self.assertNotIn("/a.py", out)
        self.assertNotIn("zzsecretzz", out)

    def test_missing_logs(self):
        self.assertEqual(contexttax.main(["--path", os.path.join(self.dir, "nope")]), 1)


if __name__ == "__main__":
    unittest.main()
