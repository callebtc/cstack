#!/usr/bin/env python3
"""Exercise launcher behavior with fake Git and reviewer CLIs; no network or model calls."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


LAUNCHER = Path(__file__).resolve().with_name("review_round.py")
BASE, HEAD = "a" * 40, "b" * 40
REPORT = """Review status: complete
## Standards
No findings.
## Spec
No findings.
## Bugs and regressions
No findings.
## Coverage
Source and both delegated review axes inspected.
Standards: 0; Spec: 0; no actionable findings.
"""
GIT_STUB = r'''
import os
from pathlib import Path
import sys
repo = Path(sys.argv[2])
args = sys.argv[3:]
mode = os.environ.get("REVIEW_TEST_MODE", "")
launched = (repo.parent / "launched.json").exists()
if args == ["rev-parse", "--show-toplevel"]:
    print(repo)
elif args == ["rev-parse", "HEAD"]:
    print("c" * 40 if mode == "mutate" and launched else "b" * 40)
elif args[:2] == ["rev-parse", "--verify"]:
    ref = args[-1].removesuffix("^{commit}")
    if ref == "missing":
        sys.exit(1)
    print(ref)
elif args[:2] == ["status", "--porcelain=v1"]:
    if mode == "dirty":
        print(" M src/feature.py")
elif args[:2] == ["diff", "--name-only"]:
    if mode != "empty":
        print("src/feature.py")
else:
    sys.exit(97)
'''
REVIEWER_STUB = r'''
import json
import os
from pathlib import Path
from select import select
import sys
import time
root = Path.cwd().parent
args = sys.argv[1:]
reviewer = Path(sys.argv[0]).name
if reviewer == "agy":
    message = json.loads(sys.stdin.readline())
    assert message["event"] == "user"
    assert not select([sys.stdin], [], [], 0.05)[0], "stdin closed before the final result"
    prompt = message["message"]["content"]
else:
    prompt = sys.stdin.read()
(root / "launched.json").write_text(json.dumps({
    "args": args, "prompt": prompt, "nested": "CLAUDECODE" in os.environ,
}))
mode = os.environ.get("REVIEW_TEST_MODE", "")
if mode == "timeout":
    time.sleep(30)
if mode == "cli-failure":
    sys.exit(3)
body = (root / "report.txt").read_text()
if mode == "incomplete":
    body = body.replace("status: complete", "status: incomplete")
if mode == "no-sections":
    body = "Review status: complete\nNo findings."
if reviewer == "codex":
    if mode != "missing-final":
        Path(args[args.index("--output-last-message") + 1]).write_text(body)
    print('{"type":"turn.completed"}')
elif mode == "bad-json":
    print("invalid JSON")
elif mode == "json-list":
    print("[]")
elif reviewer == "agy":
    model = "other-model" if mode == "wrong-model" else "gemini-4-neon-high"
    if mode != "missing-init":
        print(json.dumps({"event": "init", "init": {"model": model}}))
    print(json.dumps({"event": "step_update", "step_update": {"text_delta": body}}))
    if mode == "tool-only-turn":
        print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": ""}}), flush=True)
        continuation = json.loads(sys.stdin.readline())
        assert "Continue the same review" in continuation["message"]["content"]
        body = "Waiting for the Standards subagent.\nBoth subagents have reported.\n" + body
    if mode != "missing-final":
        status = "ERROR" if mode == "result-error" else "SUCCESS"
        print(json.dumps({"event": "result", "result": {"status": status, "response": body}}), flush=True)
        sys.stdin.read()
else:
    model = "other-model" if mode == "wrong-model" else "claude-opus-5-5"
    print(json.dumps({"subtype": "success", "is_error": mode == "result-error",
                      "modelUsage": {model: {}}, "result": body}))
'''


class ReviewRoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="external-review-launcher-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.context = self.root / "context.md"
        self.context.write_text("Requirement: preserve exact quantities. Literal: $(touch injected) $HOME")
        self.skill = self.root / "code-review/SKILL.md"
        self.skill.parent.mkdir()
        self.skill.write_text("# code-review\nStandards and Spec in parallel subagents.\n")
        (self.root / "report.txt").write_text(REPORT)
        self.output = self.root / "round"
        binary = self.root / "bin"
        binary.mkdir()
        executables = {"git": GIT_STUB, "codex": REVIEWER_STUB, "claude": REVIEWER_STUB, "agy": REVIEWER_STUB}
        for name, body in executables.items():
            path = binary / name
            path.write_text(f"#!{sys.executable}\n" + body)
            path.chmod(0o700)
        self.env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")}
        self.env.pop("REVIEW_TEST_MODE", None)

    def invoke(self, reviewer="codex", mode="", **overrides):
        (self.root / "launched.json").unlink(missing_ok=True)
        values = {
            "reviewer": reviewer, "repo": self.repo, "pr": "https://example.invalid/pulls/1",
            "base": BASE, "head": HEAD, "round": 1, "context": self.context,
            "output-dir": self.output, "review-skill": self.skill,
        }
        values.update(overrides)
        command = [sys.executable, str(LAUNCHER)]
        for key, value in values.items():
            command.append("--" + key)
            if value is not True:
                command.append(str(value))
        return subprocess.run(command, env={**self.env, "REVIEW_TEST_MODE": mode},
                              text=True, capture_output=True, timeout=10)

    def assert_not_launched(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "launched.json").exists())
        self.assertFalse(self.output.exists())

    def test_codex_pins_model_effort_and_review_context(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads((self.root / "launched.json").read_text())
        self.assertIn("gpt-6.2-sol", call["args"])
        self.assertIn('model_reasoning_effort="high"', call["args"])
        self.assertIn("read-only", call["args"])
        self.assertIn("multi_agent", call["args"])
        self.assertIn("$code-review", call["prompt"])
        self.assertIn("Read the applicable repository instructions, standards, and contracts", call["prompt"])
        self.assertIn("$(touch injected) $HOME", call["prompt"])
        self.assertIn(HEAD, call["prompt"])
        self.assertFalse((self.repo / "injected").exists())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_claude_pins_model_effort_and_separates_nested_session(self):
        self.env["CLAUDECODE"] = "1"
        result = self.invoke(reviewer="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads((self.root / "launched.json").read_text())
        self.assertFalse(call["nested"])
        self.assertIn("claude-opus-5-5", call["args"])
        self.assertEqual(call["args"][call["args"].index("--effort") + 1], "high")
        self.assertIn("plan", call["args"])
        self.assertNotIn("--dangerously-skip-permissions", call["args"])

    def test_gemini_pins_neon_high_and_streams_private_context(self):
        result = self.invoke(reviewer="gemini")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads((self.root / "launched.json").read_text())
        self.assertEqual(call["args"][:4], ["--model", "gemini-4-neon-high", "--sandbox", "--settings"])
        settings = json.loads(call["args"][4])
        self.assertEqual(settings["agentMode"], "")
        self.assertIn(rf"command(regex:^git diff {BASE}\.\.\.HEAD$)", settings["permissions"]["allow"])
        self.assertIn("write_file(*)", settings["permissions"]["deny"])
        self.assertEqual(call["args"][5:], [
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--log-file", str(self.output.resolve() / "agy.log"),
        ])
        self.assertIn("$(touch injected) $HOME", call["prompt"])
        self.assertIn("Standards and Spec in parallel subagents", call["prompt"])
        self.assertNotIn(str(self.skill), call["prompt"])
        self.assertIn(REPORT, (self.output / "review.md").read_text())

    def test_gemini_continues_a_tool_only_turn_and_extracts_the_report(self):
        result = self.invoke(reviewer="gemini", mode="tool-only-turn")
        self.assertEqual(result.returncode, 0, result.stderr)
        review = (self.output / "review.md").read_text()
        self.assertIn("Reviewer: agy; requested model: `gemini-4-neon-high`", review)
        self.assertIn(REPORT, review)
        self.assertNotIn("Waiting for the Standards subagent", review)

    def test_dry_run_prepares_review_without_project_documents(self):
        result = self.invoke(**{"dry-run": True})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "launched.json").exists())
        manifest = json.loads((self.output / "invocation.json").read_text())
        self.assertEqual(manifest["status"], "prepared")
        prompt = (self.output / "prompt.md").read_text()
        self.assertIn("Pass those sources to the Standards subagent", prompt)

    def test_followup_carries_responses_and_requires_resolution_coverage(self):
        self.context.write_text("R1-A was fixed; validate its cancellation reproduction.")
        result = self.invoke(round=2)
        self.assertEqual(result.returncode, 2)
        call = json.loads((self.root / "launched.json").read_text())
        self.assertIn("R1-A was fixed", call["prompt"])
        self.assertIn("R2-", call["prompt"])

    def test_followup_can_complete_with_resolution_coverage(self):
        (self.root / "report.txt").write_text(REPORT + "## Previous findings\nR1-A resolved.\n")
        result = self.invoke(round=2)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_inputs_never_launch(self):
        for changes, mode in [
            ({"head": "c" * 40}, ""), ({"base": "missing"}, ""),
            ({}, "dirty"), ({}, "empty"), ({"review-skill": self.root / "missing"}, ""),
            ({"output-dir": self.repo / "artifacts"}, ""),
            ({"pr": "https://user:secret@example.invalid/pulls/1"}, ""),
        ]:
            with self.subTest(changes=changes, mode=mode):
                self.assert_not_launched(self.invoke(mode=mode, **changes))

    def test_empty_context_never_launches(self):
        self.context.write_text("")
        self.assert_not_launched(self.invoke())

    def test_incomplete_reports_are_captured_without_success(self):
        result = self.invoke(mode="incomplete")
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.output / "review.md").exists())
        manifest = json.loads((self.output / "invocation.json").read_text())
        self.assertEqual(manifest["status"], "review-incomplete")

    def test_missing_sections_cannot_report_complete(self):
        self.assertEqual(self.invoke(mode="no-sections").returncode, 2)

    def test_failures_never_produce_a_publishable_review(self):
        for mode, reviewer in [
            ("cli-failure", "codex"), ("missing-final", "codex"), ("mutate", "codex"),
            ("bad-json", "claude"), ("json-list", "claude"),
            ("wrong-model", "claude"), ("result-error", "claude"),
            ("missing-init", "gemini"), ("wrong-model", "gemini"), ("missing-final", "gemini"),
            ("result-error", "gemini"), ("bad-json", "gemini"),
        ]:
            with self.subTest(mode=mode):
                output = self.root / f"{reviewer}-{mode}"
                result = self.invoke(reviewer=reviewer, mode=mode, **{"output-dir": output})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse((output / "review.md").exists())
                manifest = json.loads((output / "invocation.json").read_text())
                self.assertEqual(manifest["status"], "review-incomplete")
                self.assertNotIn("Traceback", result.stderr)

    def test_timeout_ends_the_reviewer_process(self):
        for reviewer in ("codex", "gemini"):
            with self.subTest(reviewer=reviewer):
                output = self.root / f"{reviewer}-timeout"
                result = self.invoke(reviewer=reviewer, mode="timeout", **{
                    "output-dir": output, "timeout-seconds": 1,
                })
                self.assertEqual(result.returncode, 1)
                self.assertIn("timed out", result.stderr)
                self.assertFalse((output / "review.md").exists())


if __name__ == "__main__":
    unittest.main()
