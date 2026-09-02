#!/usr/bin/env python3
"""Migrate Claude Code and Codex configuration into Sarvam Code.

Handles: skills (frontmatter repair + discovery), slash commands (-> skills),
MCP servers (JSON/TOML -> Sarvam TOML), durable memories (curated stores ->
~/.sarvam/memories), and AGENTS.md hard-rule seeding.

Dry-run by default; --apply to execute; --copy-mode to copy skills instead of
symlinking them. Never reads or copies secret files (secrets.json,
.credentials.json, auth.json, keyring). MCP env values are written to the
target config but are always REDACTED in plan output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

try:
    import tomllib  # py>=3.11
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None

# ---------------------------------------------------------------- constants

SECRET_FILE_NAMES = {
    "secrets.json", ".credentials.json", "auth.json", "keyring",
    ".cookiejar", "credentials.json",
}

# MCP servers tied to a specific app's binaries; useless elsewhere.
MCP_SKIP_NAMES = {"node_repl", "computer-use"}
MCP_SKIP_COMMAND_FRAGMENTS = ("ChatGPT.app", "Codex Computer Use", "SkyComputerUseClient")

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

AGENTS_TEMPLATE = """\
# Global Agent Instructions

## Hard Rules (never violate)

These are the non-negotiable, outward-facing / irreversible rules — always in-context. Fuller preferences live in the memory store (see Memory below).

- **Approvals & outward actions:** Never execute approvals/acceptances or any action that notifies real people (emails, WhatsApp, sends, bulk ops) without an explicit per-batch "go ahead". Momentum and prior batches do NOT carry consent forward.
- **Secrets:** Never expose secret values in chat, docs, commits, PRs, or logs. Secrets live in the vault (`~/.claude/secrets.json`); reference them, never print them.
- **Company creds isolation:** Company credentials NEVER go into personal projects.
- **Git:** Never push WIP to main — branch + PR. Every PR is reviewed before merge.
- **Identifier mismatch:** If an identifier (email/phone/name) matches nobody, ASK before fuzzy-matching.

## Memory

Before claiming something is missing (credentials, webhooks, analytics, cloud access), read the memory index then the matching memory file:

- **Sarvam Code:** `~/.sarvam/memories/user/INDEX.md`, then the matching file under `~/.sarvam/memories/user/` (also `project/` and `reference/` subdirectories).
- **Claude Code / Codex:** `~/.codex/memories/claude-context-current/INDEX.md`, then the matching `project_*` / `reference_*` file.
- Secrets are in `~/.claude/secrets.json` — fetch, never ask.

## Learnings

When working in any project, check for `learnings.md` at the repository root for project-specific learnings. Cross-project learnings live in the memory store above.
"""

AGENTS_HARD_RULES_MARKER = "## Hard Rules"
AGENTS_APPEND_TEMPLATE = """

<!-- BEGIN sarvam-migration hard rules (added {date}) -->

{body}

<!-- END sarvam-migration hard rules -->
"""


# ---------------------------------------------------------------- helpers

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def files_identical(a: Path, b: Path) -> bool:
    if not (a.is_file() and b.is_file()):
        return False
    if a.stat().st_size != b.stat().st_size:
        return False
    return sha256(a) == sha256(b)


def redact(value: str) -> str:
    """Redact anything that looks like a credential in plan output."""
    if not value:
        return value
    if any(tok in value for tok in ("mongodb+srv://", "://", "key", "token", "secret", "password")):
        return "<redacted>"
    if len(value) > 24:
        return "<redacted>"
    return value


class Plan:
    """Ordered list of operations with dry-run printing and apply."""

    def __init__(self) -> None:
        self.ops: list[dict] = []

    def add(self, kind: str, path: Path, detail: str, payload: dict | None = None) -> None:
        self.ops.append({"kind": kind, "path": path, "detail": detail, "payload": payload or {}})

    def skip(self, detail: str) -> None:
        self.ops.append({"kind": "skip", "path": None, "detail": detail, "payload": {}})

    def print_plan(self) -> None:
        if not self.ops:
            print("Nothing to do — sources already migrated or absent.")
            return
        n_do = 0
        for op in self.ops:
            if op["kind"] == "skip":
                print(f"  skip   {op['detail']}")
                continue
            n_do += 1
            print(f"  {op['kind']:<7} {op['path']}")
            if op["detail"]:
                print(f"          {op['detail']}")
        print(f"\n{n_do} operation(s) would run.")

    def apply(self) -> int:
        done = 0
        for op in self.ops:
            if op["kind"] == "skip":
                continue
            self._apply_op(op)
            done += 1
        return done

    def _apply_op(self, op: dict) -> None:
        kind, path, payload = op["kind"], op["path"], op["payload"]
        if kind == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
        elif kind == "copy":
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(payload["src"], path)
        elif kind == "symlink":
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.exists():
                return
            path.symlink_to(payload["target"])
        elif kind == "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            _backup_if_exists(path)
            path.write_text(payload["content"], encoding="utf-8")
        elif kind == "edit":
            path.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            path.write_text(payload["transform"](text), encoding="utf-8")
        else:
            raise ValueError(f"unknown op kind: {kind}")


def _backup_if_exists(path: Path) -> None:
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- detection

def detect_sources(home: Path) -> dict:
    return {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "agents": home / ".agents",
        "sarvam": home / ".sarvam",
    }


def sarvam_paths(home: Path) -> dict:
    sarvam = home / ".sarvam"
    return {
        "skills": sarvam / "skills",
        "config": sarvam / "config.toml",
        "memories_user": sarvam / "memories" / "user",
        "memories_ext": sarvam / "memories" / "extensions",
    }


# ---------------------------------------------------------------- skills

def has_frontmatter(text: str) -> bool:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return False
    return re.search(r"^\s*name\s*:", m.group(1), re.MULTILINE) is not None


def derive_description(text: str) -> str:
    m = re.search(r"##\s*Description\s*\n(.+?)(?=\n##|\Z)", text, re.DOTALL)
    if m:
        desc = " ".join(m.group(1).split())
    else:
        body = FRONTMATTER_RE.sub("", text, count=1) if FRONTMATTER_RE.match(text) else text
        lines = [ln.strip().lstrip("#- ") for ln in body.splitlines() if ln.strip()]
        desc = lines[0] if lines else ""
    desc = desc.rstrip(".")
    return (desc[:197] + "...") if len(desc) > 200 else desc


def plan_skill_repair(home: Path, plan: Plan) -> None:
    """Add frontmatter to SKILL.md files that lack it (any source root)."""
    sources = detect_sources(home)
    for root_name in ("claude", "codex", "agents"):
        skills_dir = sources[root_name] / "skills"
        if not skills_dir.is_dir():
            continue
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            if "backup" in skill_dir.name.lower():
                plan.skip(f"skill backup dir {skill_dir.name} ignored")
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            text = read_text(skill_md)
            if has_frontmatter(text):
                continue
            name = skill_dir.name
            desc = derive_description(text).replace('"', "'")
            fm = f'---\nname: {name}\ndescription: "{desc}"\n---\n\n'
            plan.add(
                "edit", skill_md,
                f"prepend frontmatter (name: {name})",
                {"transform": lambda t, _fm=fm: _fm + t},
            )


def plan_skill_discovery(home: Path, plan: Plan, copy_mode: bool) -> None:
    """Make every source skill discoverable under ~/.sarvam/skills.

    Sarvam natively scans ~/.claude/skills, ~/.codex/skills and
    ~/.agents/skills in current builds, but linking into ~/.sarvam/skills
    guarantees discovery everywhere. Idempotent: existing names are kept.
    """
    sources = detect_sources(home)
    target_skills = sarvam_paths(home)["skills"]
    existing = {p.name for p in target_skills.iterdir()} if target_skills.is_dir() else set()
    for root_name in ("claude", "codex", "agents"):
        skills_dir = sources[root_name] / "skills"
        if not skills_dir.is_dir():
            continue
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith(".") or skill_dir.name in existing:
                continue
            if not (skill_dir / "SKILL.md").is_file():
                continue
            if "backup" in skill_dir.name.lower():
                continue
            existing.add(skill_dir.name)
            target = target_skills / skill_dir.name
            if copy_mode:
                plan.add("copy", target, f"copy skill from {skill_dir}", {"src": str(skill_dir)})
            else:
                plan.add("symlink", target, f"link skill from {skill_dir}", {"target": str(skill_dir)})


# ---------------------------------------------------------------- commands

def plan_command_conversion(home: Path, plan: Plan) -> None:
    """Convert Claude/Codex slash commands and prompts into Sarvam skills."""
    sources = detect_sources(home)
    target_skills = sarvam_paths(home)["skills"]
    existing_names = set()
    for root in (target_skills, sources["claude"] / "skills", sources["codex"] / "skills",
                 sources["agents"] / "skills"):
        if root.is_dir():
            existing_names |= {p.name for p in root.iterdir() if p.is_dir()}

    command_files: list[tuple[Path, str]] = []
    for root_name in ("claude", "codex"):
        cmd_dir = sources[root_name] / "commands"
        if cmd_dir.is_dir():
            command_files += [(f, "command") for f in sorted(cmd_dir.glob("*.md"))]
    prompts_dir = sources["codex"] / "prompts"
    if prompts_dir.is_dir():
        command_files += [(f, "prompt") for f in sorted(prompts_dir.glob("*.md"))]

    for cmd_path, origin in command_files:
        name = cmd_path.stem.lower().replace(" ", "-")
        text = read_text(cmd_path)
        fm = FRONTMATTER_RE.match(text)
        desc = ""
        if fm:
            m = re.search(r"^\s*description\s*:\s*(.+)$", fm.group(1), re.MULTILINE)
            if m:
                desc = m.group(1).strip().strip('"')
        if not desc:
            first_h1 = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
            desc = first_h1.group(1).strip() if first_h1 else name

        # A prompt that just wraps an existing skill needs no new skill.
        m = re.search(r"\$([a-z0-9-]+)\s+skill", text, re.IGNORECASE)
        if m and m.group(1).lower() in {n.lower() for n in existing_names}:
            plan.skip(f"{origin} {cmd_path.name} wraps existing skill '{m.group(1)}' — no conversion needed")
            continue
        if name in existing_names:
            plan.skip(f"skill '{name}' already exists — {origin} {cmd_path.name} not converted")
            continue

        body = text[fm.end():] if fm else text
        body = body.replace("AskUserQuestion", "request_user_input")
        body = body.replace("$ARGUMENTS", "<the arguments the user passed after the skill name>")
        content = (
            f'---\nname: {name}\n'
            f'description: "{desc[:197]} (converted from {origin} {cmd_path.name})"\n---\n\n'
            f"<!-- migrated from {cmd_path} -->\n"
            + body.strip() + "\n"
        )
        existing_names.add(name)
        plan.add("write", target_skills / name / "SKILL.md",
                 f"convert {origin} {cmd_path.name} to skill '{name}'",
                 {"content": content})


# ---------------------------------------------------------------- mcp

def load_toml(path: Path) -> dict:
    if tomllib is None:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def plan_mcp(home: Path, plan: Plan) -> None:
    """Merge Claude (settings.json mcpServers) + Codex (config.toml
    mcp_servers) into ~/.sarvam/config.toml. Env values redacted in output."""
    sources = detect_sources(home)
    target_cfg = sarvam_paths(home)["config"]

    incoming: dict[str, dict] = {}
    claude_settings = sources["claude"] / "settings.json"
    if claude_settings.is_file():
        try:
            data = json.loads(read_text(claude_settings))
            for name, cfg in (data.get("mcpServers") or {}).items():
                incoming[name] = _normalize_mcp(cfg, stdio=True)
        except (json.JSONDecodeError, OSError) as exc:
            plan.skip(f"could not parse {claude_settings}: {exc}")

    codex_cfg = sources["codex"] / "config.toml"
    if codex_cfg.is_file():
        if tomllib is None:
            plan.skip("tomllib/tomli unavailable — install 'tomli' to migrate Codex MCP servers")
        else:
            try:
                data = load_toml(codex_cfg)
                for name, cfg in (data.get("mcp_servers") or {}).items():
                    incoming[name] = _normalize_mcp(cfg, stdio="command" in cfg)
            except Exception as exc:  # noqa: BLE001
                plan.skip(f"could not parse {codex_cfg}: {exc}")

    existing: dict[str, dict] = {}
    if target_cfg.is_file():
        try:
            existing = (load_toml(target_cfg) or {}).get("mcp_servers") or {}
        except Exception as exc:  # noqa: BLE001
            plan.skip(f"could not parse {target_cfg}: {exc} — refusing to append")

    to_add: dict[str, dict] = {}
    for name, cfg in incoming.items():
        if name in existing:
            plan.skip(f"mcp server '{name}' already in Sarvam config")
            continue
        cmd = cfg.get("command", "") or ""
        url = cfg.get("url", "") or ""
        if name in MCP_SKIP_NAMES or any(frag in cmd for frag in MCP_SKIP_COMMAND_FRAGMENTS):
            plan.skip(f"mcp server '{name}' is app-specific (ChatGPT/Codex binary) — not migrated")
            continue
        if not cmd and not url:
            plan.skip(f"mcp server '{name}' has neither command nor url — skipped")
            continue
        to_add[name] = cfg

    if not to_add:
        return

    def transform(text: str, _add=to_add) -> str:
        block = "\n\n# --- MCP servers migrated from Claude Code / Codex ---\n"
        for name, cfg in _add.items():
            block += f"\n[mcp_servers.{name}]\n"
            if cfg.get("command"):
                block += f"command = {json.dumps(cfg['command'])}\n"
            if cfg.get("args"):
                block += "args = " + json.dumps(cfg["args"]) + "\n"
            if cfg.get("url"):
                block += f"url = {json.dumps(cfg['url'])}\n"
            if cfg.get("env"):
                block += f"\n[mcp_servers.{name}.env]\n"
                for k, v in cfg["env"].items():
                    block += f"{json.dumps(k)} = {json.dumps(v)}\n"
        if not text.endswith("\n"):
            text += "\n"
        return text + block

    names = ", ".join(sorted(to_add))
    plan.add("edit", target_cfg, f"append mcp_servers tables: {names} (env values redacted in this output)",
             {"transform": transform})

    # Echo the env KEYS only — never values.
    for name, cfg in sorted(to_add.items()):
        if cfg.get("env"):
            plan.skip(f"mcp '{name}' env keys carried over: {', '.join(sorted(cfg['env']))} (values written to config, not shown)")


def _normalize_mcp(cfg: dict, stdio: bool) -> dict:
    out: dict = {}
    if stdio:
        if cfg.get("command"):
            out["command"] = cfg["command"]
        if cfg.get("args"):
            out["args"] = list(cfg["args"])
    elif cfg.get("url"):
        out["url"] = cfg["url"]
    env = cfg.get("env") or {}
    if env:
        out["env"] = dict(env)
    return out


# ---------------------------------------------------------------- memories

def curated_memory_dir(home: Path) -> Path | None:
    """Locate the hand-curated durable memory store, newest convention first."""
    candidates = [
        home / ".codex" / "memories" / "claude-context-current",
        home / ".claude" / "projects" / ("-Users-" + home.name) / "memory",
    ]
    for c in candidates:
        if c.is_dir() and any(c.glob("*.md")):
            return c
    return None


def plan_memories(home: Path, plan: Plan) -> None:
    sources = detect_sources(home)
    paths = sarvam_paths(home)
    mem_user = paths["memories_user"]

    src = curated_memory_dir(home)
    if src is None:
        plan.skip("no curated memory store found (claude-context-current or Claude project memory)")
    else:
        already = mem_user.is_dir() and any(mem_user.glob("*.md"))
        if already:
            plan.skip(f"memory store already present at {mem_user} — leaving in place")
        else:
            plan.add("mkdir", mem_user, "create user memory root")
            plan.add("mkdir", mem_user / "project", "project_* mirror")
            plan.add("mkdir", mem_user / "reference", "reference_* mirror")
            for f in sorted(src.glob("*.md")):
                plan.add("copy", mem_user / f.name, f"memory file from {src.name}/{f.name}",
                         {"src": str(f)})
                if f.name.startswith("project_"):
                    plan.add("copy", mem_user / "project" / f.name, "project mirror", {"src": str(f)})
                elif f.name.startswith("reference_"):
                    plan.add("copy", mem_user / "reference" / f.name, "reference mirror", {"src": str(f)})
            # Codex keeps a second, live task-group index at memories/MEMORY.md.
            top_memory = sources["codex"] / "memories" / "MEMORY.md"
            if top_memory.is_file() and not files_identical(top_memory, src / "MEMORY.md"):
                plan.add("copy", mem_user / "MEMORY-task-groups.md",
                         "Codex live task-group index (distinct from curated MEMORY.md)",
                         {"src": str(top_memory)})

    rollouts = sources["codex"] / "memories" / "rollout_summaries"
    if rollouts.is_dir():
        target = mem_user / "rollout_summaries"
        have = {p.name for p in target.iterdir()} if target.is_dir() else set()
        for f in sorted(rollouts.glob("*.md")):
            if f.name in have:
                continue
            plan.add("copy", target / f.name, "rollout summary", {"src": str(f)})

    adhoc = sources["codex"] / "memories" / "extensions" / "ad_hoc"
    if adhoc.is_dir():
        target = paths["memories_ext"] / "ad_hoc" / "notes"
        have = {p.name for p in target.iterdir()} if target.is_dir() else set()
        instr = adhoc / "instructions.md"
        if instr.is_file() and not (paths["memories_ext"] / "ad_hoc" / "instructions.md").is_file():
            plan.add("copy", paths["memories_ext"] / "ad_hoc" / "instructions.md",
                     "ad-hoc extension instructions", {"src": str(instr)})
        for f in sorted(adhoc.glob("notes/*.md")):
            if f.name in have:
                continue
            plan.add("copy", target / f.name, "ad-hoc note", {"src": str(f)})


# ---------------------------------------------------------------- agents.md

def plan_agents_md(home: Path, plan: Plan) -> None:
    agents = home / "AGENTS.md"
    if not agents.is_file():
        plan.add("write", agents, "seed AGENTS.md with hard rules + memory pointers",
                 {"content": AGENTS_TEMPLATE})
        return
    text = read_text(agents)
    if AGENTS_HARD_RULES_MARKER in text:
        plan.skip("AGENTS.md already has hard rules — left unchanged")
        return
    body = "\n".join(line for line in AGENTS_TEMPLATE.splitlines()
                     if not line.startswith("# ")).strip()
    content = text.rstrip() + AGENTS_APPEND_TEMPLATE.format(
        date=time.strftime("%Y-%m-%d"), body=body)
    plan.add("edit", agents, "append hard rules + memory pointers (existing content preserved)",
             {"transform": lambda _t, _c=content: _c})


# ---------------------------------------------------------------- main

def build_plan(home: Path, copy_mode: bool) -> Plan:
    plan = Plan()
    sources = detect_sources(home)
    if not sources["sarvam"].is_dir():
        plan.add("mkdir", sources["sarvam"], "create ~/.sarvam")
    if not sarvam_paths(home)["skills"].is_dir():
        plan.add("mkdir", sarvam_paths(home)["skills"], "create user skills root")

    plan_skill_repair(home, plan)
    plan_skill_discovery(home, plan, copy_mode)
    plan_command_conversion(home, plan)
    plan_mcp(home, plan)
    plan_memories(home, plan)
    plan_agents_md(home, plan)
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=Path.home(),
                    help="home directory to migrate (default: real home; for testing)")
    ap.add_argument("--apply", action="store_true",
                    help="execute the plan (default: dry run)")
    ap.add_argument("--copy-mode", action="store_true",
                    help="copy skills into ~/.sarvam/skills instead of symlinking")
    args = ap.parse_args(argv)

    home = args.home.expanduser().resolve()
    if home == Path.home().resolve():
        pass  # real home — fine
    elif not home.is_dir():
        print(f"error: --home {home} is not a directory", file=sys.stderr)
        return 2

    plan = build_plan(home, args.copy_mode)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"Sarvam migration — {mode} — home: {home}\n")
    plan.print_plan()

    if args.apply:
        n = plan.apply()
        print(f"\nApplied {n} operation(s). Restart Sarvam Code, then run /skills and /mcp to verify.")
    else:
        print("\nDry run only. Re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
