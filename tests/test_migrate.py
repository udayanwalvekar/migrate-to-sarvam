#!/usr/bin/env python3
"""End-to-end tests for the migrate-to-sarvam script.

Builds a synthetic home directory exercising every migration path (legacy
skills, command/prompt conversion, MCP from JSON+TOML, curated memories,
AGENTS.md seed/append), then asserts on dry-run plans, apply results,
idempotency, and secret hygiene. Stdlib only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib  # py>=3.11
except ImportError:
    import tomli as tomllib  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "skills" / "migrate-to-sarvam" / "scripts" / "migrate.py"

SECRET = "mongodb+srv://user:supersecret@host.example/db"


def run_script(home: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--home", str(home), *flags],
        capture_output=True, text=True, timeout=120,
    )


def make_fakehome(root: Path, *, with_agents_md: bool = True,
                  with_existing_sarvam: bool = False) -> Path:
    home = root / "home"
    # Claude side
    old = home / ".claude/skills/old-skill"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text(
        "# Skill: Old Skill\n\n## Description\nDoes the old thing very well.\n\n## Steps\n- step one\n")
    (home / ".claude/settings.json").write_text(json.dumps({
        "mcpServers": {
            "mongodb": {"type": "stdio", "command": "mongodb-mcp-server",
                        "args": ["--readOnly"], "env": {"MDB_MCP_CONNECTION_STRING": SECRET}},
            "chatgpt-thing": {"type": "stdio",
                              "command": "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl",
                              "args": []},
        }
    }))
    (home / ".claude/commands").mkdir(parents=True)
    (home / ".claude/commands/deploy-check.md").write_text(
        "# Deploy Check\n\nAskUserQuestion for the target env, then check $ARGUMENTS deployment status.\n")
    # Codex side
    legacy = home / ".codex/skills/legacy-tool"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# Skill: Legacy Tool\n\n## Description\nA tool from the before-times.\n")
    learn = home / ".codex/skills/learn"
    learn.mkdir(parents=True)
    (learn / "SKILL.md").write_text("---\nname: learn\ndescription: Draft session learnings\n---\n\n# Learn\n")
    (home / ".codex/commands").mkdir(parents=True)
    (home / ".codex/prompts").mkdir(parents=True)
    (home / ".codex/prompts/learn.md").write_text(
        "---\ndescription: Draft learnings (runs the learn skill)\n---\n\n"
        "Use the $learn skill: read /x/SKILL.md and follow it. Focus: $ARGUMENTS\n")
    (home / ".codex/config.toml").write_text(
        'model = "test"\n'
        "[mcp_servers.mongodb]\n"
        'command = "mongodb-mcp-server"\n'
        'args = ["--readOnly"]\n'
        "[mcp_servers.mongodb.env]\n"
        f'MDB_MCP_CONNECTION_STRING = "{SECRET}"\n'
        "[mcp_servers.node_repl]\n"
        'command = "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl"\n')
    # Memories
    cur = home / ".codex/memories/claude-context-current"
    cur.mkdir(parents=True)
    (cur / "INDEX.md").write_text("# Index\n- feedback_test.md — test memory\n")
    (cur / "feedback_test.md").write_text("---\nname: test\ndescription: test memory\n---\n\nAlways test.\n")
    (cur / "project_alpha.md").write_text("project alpha state\n")
    (cur / "reference_beta.md").write_text("reference beta\n")
    (cur / "MEMORY.md").write_text("# Memory Index (curated)\n")
    (home / ".codex/memories/MEMORY.md").write_text("# Task Group: live codex index\n")
    ro = home / ".codex/memories/rollout_summaries"
    ro.mkdir(parents=True)
    (ro / "2026-01-01-test.md").write_text("rollout summary test\n")
    notes = home / ".codex/memories/extensions/ad_hoc/notes"
    notes.mkdir(parents=True)
    (home / ".codex/memories/extensions/ad_hoc/instructions.md").write_text("instructions\n")
    (notes / "2026-01-01-note.md").write_text("ad hoc note\n")
    # AGENTS.md
    if with_agents_md:
        (home / "AGENTS.md").write_text("# My existing instructions\n\nBe nice.\n")
    # Pre-existing Sarvam config
    if with_existing_sarvam:
        cfg = home / ".sarvam"
        cfg.mkdir(parents=True)
        (cfg / "config.toml").write_text(
            '[projects."/tmp/somewhere"]\ntrust_level = "trusted"\n\n'
            "[mcp_servers.existing]\n"
            'command = "existing-server"\n')
    return home


class MigrationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = make_fakehome(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestDryRun(MigrationTestBase):
    def test_dry_run_creates_nothing(self):
        r = run_script(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("operation(s) would run", r.stdout)
        self.assertFalse((self.home / ".sarvam").exists(), "dry run must not create ~/.sarvam")
        self.assertNotIn("frontmatter", (self.home / ".claude/skills/old-skill/SKILL.md").read_text())

    def test_plan_output_contains_no_secrets(self):
        r = run_script(self.home)
        self.assertNotIn("supersecret", r.stdout)
        self.assertNotIn("supersecret", r.stderr)
        self.assertIn("redacted", r.stdout)


class TestApply(MigrationTestBase):
    def setUp(self):
        super().setUp()
        r = run_script(self.home, "--apply")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.apply_output = r.stdout

    def test_frontmatter_added_with_backup(self):
        text = (self.home / ".claude/skills/old-skill/SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: old-skill"))
        backups = list((self.home / ".claude/skills/old-skill").glob("SKILL.md.bak-*"))
        self.assertEqual(len(backups), 1, "frontmatter edit must leave exactly one backup")

    def test_skills_linked_into_sarvam(self):
        skills = self.home / ".sarvam/skills"
        for name in ("old-skill", "legacy-tool", "learn"):
            self.assertTrue((skills / name).is_symlink(), f"{name} should be a symlink")

    def test_command_converted_with_tool_swaps(self):
        text = (self.home / ".sarvam/skills/deploy-check/SKILL.md").read_text()
        self.assertIn("name: deploy-check", text)
        self.assertIn("request_user_input", text)
        self.assertNotIn("AskUserQuestion", text)
        self.assertNotIn("$ARGUMENTS", text)

    def test_thin_prompt_wrapper_skipped(self):
        # learn.md only wraps the existing learn skill -> no converted copy
        learn = self.home / ".sarvam/skills/learn"
        self.assertTrue(learn.is_symlink(), "learn must stay a symlink to the real skill")
        self.assertIn("Draft session learnings", (self.home / ".codex/skills/learn/SKILL.md").read_text())

    def test_mcp_migrated_and_app_specific_skipped(self):
        cfg_path = self.home / ".sarvam/config.toml"
        data = tomllib.loads(cfg_path.read_text())
        servers = data["mcp_servers"]
        self.assertIn("mongodb", servers)
        self.assertEqual(servers["mongodb"]["args"], ["--readOnly"])
        self.assertNotIn("node_repl", servers)
        self.assertNotIn("chatgpt-thing", servers)

    def test_env_value_written_to_config_but_not_to_output(self):
        cfg = (self.home / ".sarvam/config.toml").read_text()
        self.assertIn(SECRET, cfg)  # servers read env from config; that is where it belongs
        self.assertNotIn("supersecret", self.apply_output)

    def test_memories_copied_with_mirrors(self):
        user = self.home / ".sarvam/memories/user"
        for rel in ("INDEX.md", "feedback_test.md", "project_alpha.md", "reference_beta.md",
                    "project/project_alpha.md", "reference/reference_beta.md",
                    "MEMORY-task-groups.md", "rollout_summaries/2026-01-01-test.md"):
            self.assertTrue((user / rel).is_file(), f"missing {rel}")
        self.assertTrue((self.home / ".sarvam/memories/extensions/ad_hoc/notes/2026-01-01-note.md").is_file())

    def test_memory_content_identical(self):
        src = self.home / ".codex/memories/claude-context-current/feedback_test.md"
        dst = self.home / ".sarvam/memories/user/feedback_test.md"
        self.assertEqual(src.read_bytes(), dst.read_bytes())

    def test_agents_md_append_preserves_existing(self):
        text = (self.home / "AGENTS.md").read_text()
        self.assertIn("# My existing instructions", text)
        self.assertIn("Be nice.", text)
        self.assertIn("## Hard Rules", text)
        self.assertEqual(text.count("BEGIN sarvam-migration"), 1)


class TestIdempotency(MigrationTestBase):
    def test_second_apply_is_noop(self):
        self.assertEqual(run_script(self.home, "--apply").returncode, 0)
        r = run_script(self.home, "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Applied 0 operation(s)", r.stdout)
        text = (self.home / "AGENTS.md").read_text()
        self.assertEqual(text.count("BEGIN sarvam-migration"), 1, "must not double-append")
        backups = list((self.home / ".claude/skills/old-skill").glob("SKILL.md.bak-*"))
        self.assertEqual(len(backups), 1, "must not re-edit an already-valid SKILL.md")


class TestAgentsSeed(MigrationTestBase):
    def test_seeds_when_missing(self):
        (self.home / "AGENTS.md").unlink()
        r = run_script(self.home, "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.home / "AGENTS.md").read_text()
        self.assertIn("# Global Agent Instructions", text)
        self.assertIn("## Hard Rules", text)
        self.assertIn("## Memory", text)


class TestCopyMode(MigrationTestBase):
    def test_copy_mode_copies_instead_of_symlinking(self):
        r = run_script(self.home, "--copy-mode", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        target = self.home / ".sarvam/skills/old-skill"
        self.assertFalse(target.is_symlink())
        self.assertTrue((target / "SKILL.md").is_file())


class TestExistingConfigPreserved(MigrationTestBase):
    def test_existing_servers_and_projects_kept(self):
        home = make_fakehome(self.root / "existing-case", with_existing_sarvam=True)
        r = run_script(home, "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = tomllib.loads((home / ".sarvam/config.toml").read_text())
        self.assertIn("existing", data["mcp_servers"])
        self.assertIn("mongodb", data["mcp_servers"])
        self.assertIn("/tmp/somewhere", data["projects"])


class TestSkillIntegrity(unittest.TestCase):
    def test_skill_md_has_valid_frontmatter(self):
        text = (REPO_ROOT / "skills/migrate-to-sarvam/SKILL.md").read_text()
        m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        self.assertIsNotNone(m, "SKILL.md must open with YAML frontmatter")
        self.assertRegex(m.group(1), r"(?m)^name:\s*migrate-to-sarvam")
        self.assertRegex(m.group(1), r"(?m)^description:\s*\S")

    def test_no_personal_data_in_skill(self):
        skill_dir = REPO_ROOT / "skills/migrate-to-sarvam"
        for f in skill_dir.rglob("*"):
            if f.is_file():
                body = f.read_text(encoding="utf-8", errors="replace").lower()
                for token in ("udayan", "walvekar", "growthx", "gx-", "supersecret"):
                    self.assertNotIn(token, body, f"{f.name} contains '{token}'")


if __name__ == "__main__":
    unittest.main()
