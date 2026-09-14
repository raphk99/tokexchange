import json
import os
from pathlib import Path

from tokexchange import context as ctx
from tests.helpers import IsolatedEnv


def _line(**kw):
    return json.dumps(kw)


class ContextTests(IsolatedEnv):
    def test_parse_transcript_keeps_prose_only(self):
        lines = [
            _line(type="mode", mode="normal", sessionId="s1"),
            _line(type="user", sessionId="s1", cwd="/x", gitBranch="main", origin={"kind": "human"},
                  message={"role": "user", "content": "Please add a login page <system-reminder>secret</system-reminder>"}),
            _line(type="assistant", sessionId="s1", message={"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Sure, I will add it."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}),
            _line(type="user", sessionId="s1", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "file1 file2"}]}),
            _line(type="user", sessionId="s1", isSidechain=True, message={"role": "user", "content": "subagent prompt"}),
            _line(type="user", sessionId="s1", isMeta=True, message={"role": "user", "content": "meta"}),
            _line(type="user", sessionId="s1", origin={"kind": "human"}, message={"role": "user", "content": [
                {"type": "text", "text": "Use OAuth, not passwords."}]}),
            "not json",
        ]
        d = ctx.parse_transcript(lines)
        self.assertEqual(d.session_id, "s1")
        self.assertEqual(d.git_branch, "main")
        self.assertEqual([t.role for t in d.turns], ["user", "assistant", "user"])
        self.assertEqual(d.turns[0].text, "Please add a login page")
        self.assertEqual(d.turns[1].text, "Sure, I will add it.")
        self.assertEqual(d.turns[2].text, "Use OAuth, not passwords.")

    def test_budget_keeps_first_and_recent(self):
        turns = [ctx.Turn("user", "first request")] + [ctx.Turn("assistant", f"reply {i} " + "x" * 100) for i in range(20)]
        kept, truncated = ctx.budget_turns(turns, max_chars=500)
        self.assertTrue(truncated)
        self.assertEqual(kept[0].text, "first request")
        self.assertEqual(kept[-1].text, turns[-1].text)
        self.assertLess(sum(len(t.text) for t in kept), 600)

    def test_find_transcript_prefers_env_session(self):
        cwd = Path("/Users/me/Desktop/code/proj")
        d = ctx.project_transcript_dir(cwd)
        self.assertEqual(d.name, "-Users-me-Desktop-code-proj")
        d.mkdir(parents=True)
        (d / "old.jsonl").write_text("{}\n")
        (d / "current.jsonl").write_text("{}\n")
        os.utime(d / "old.jsonl", (1, 1))
        self.assertEqual(ctx.find_transcript(cwd).name, "current.jsonl")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "old"
        self.assertEqual(ctx.find_transcript(cwd).name, "old.jsonl")
        self.assertEqual(ctx.find_transcript(cwd, "current").name, "current.jsonl")
        self.assertIsNone(ctx.find_transcript(Path("/nope")))

    def test_render_brief_and_prompt(self):
        digest = ctx.Digest(turns=[ctx.Turn("user", "add login"), ctx.Turn("assistant", "ok")])
        brief = ctx.render_brief(request="finish login", repo={"branch": "main", "base_commit": "abc", "has_uncommitted_patch": True},
                                 digest=digest, notes=["we chose OAuth"], criteria=["works"], tests=["make test"],
                                 constraints=["no new deps"], attached_files=["docs/spec.md"])
        for needle in ("we chose OAuth", "`make test`", "no new deps", "docs/spec.md", "### Developer", "add login", "finish login", "baseline"):
            self.assertIn(needle, brief)
        prompt = ctx.render_prompt(request="finish login", tests=["make test"], criteria=["works"])
        self.assertIn("finish login", prompt)
        self.assertIn("Do not push", prompt)
        self.assertIn("`make test`", prompt)
