---
name: migrate-to-sarvam
description: Migrate a user's Claude Code and Codex configuration into Sarvam Code — skills, slash commands, MCP servers, durable memories, and AGENTS.md hard rules. Use when the user asks to migrate/import/move their Claude Code or Codex setup, skills, memory, or MCP config to Sarvam Code, or asks "can you use my Claude/Codex skills here".
---

# Migrate Claude Code / Codex to Sarvam Code

Bring another agent tool's accumulated setup into Sarvam Code: skills, custom
commands, MCP servers, durable memories, and global instructions. The script
does the mechanical work; you make the judgment calls and verify.

## What migrates, and how

| Source | Sarvam destination | Mechanism |
|--------|--------------------|-----------|
| `~/.claude/skills/`, `~/.codex/skills/`, `~/.agents/skills/` | discovered in place + linked into `~/.sarvam/skills/` | symlink (or copy with `--copy-mode`) |
| `SKILL.md` files missing frontmatter | same file, frontmatter prepended | in-place edit (backed up first) |
| `~/.claude/commands/*.md`, `~/.codex/commands/*.md`, `~/.codex/prompts/*.md` | `~/.sarvam/skills/<name>/SKILL.md` | converted to skills |
| Claude `settings.json` `mcpServers` + Codex `config.toml` `mcp_servers` | `~/.sarvam/config.toml` `[mcp_servers.*]` | TOML append |
| Curated memory store (`~/.codex/memories/claude-context-current/`, or Claude's per-project memory) | `~/.sarvam/memories/user/` (+ `project/`, `reference/` mirrors) | file copy, byte-identical |
| `~/.codex/memories/rollout_summaries/`, `extensions/ad_hoc/` | `~/.sarvam/memories/user/rollout_summaries/`, `~/.sarvam/memories/extensions/ad_hoc/notes/` | file copy |
| `~/AGENTS.md` | same file | seeded or appended with hard rules + memory pointers |

## Workflow

1. **Survey first.** List what exists before proposing anything:
   ```bash
   ls ~/.claude/skills/ ~/.codex/skills/ ~/.agents/skills/ 2>/dev/null
   ls ~/.claude/commands/ ~/.codex/commands/ ~/.codex/prompts/ 2>/dev/null
   python3 -c "import json;d=json.load(open('$HOME/.claude/settings.json'));print(list(d.get('mcpServers',{})))" 2>/dev/null
   grep -A2 '^\[mcp_servers' ~/.codex/config.toml 2>/dev/null
   du -sh ~/.codex/memories/* 2>/dev/null
   ```
2. **Run the script dry (it is dry-run by default):**
   ```bash
   python3 <skill_dir>/scripts/migrate.py
   ```
   Read the plan line by line. Every `skip` line is a judgment the script made —
   sanity-check them.
3. **Confirm scope with the user** before `--apply`. The two decisions that
   matter: skills symlinked vs copied (`--copy-mode`), and how much memory to
   bring over (the script migrates the curated store; machine-generated
   extension stores like chronicle/skysight are deliberately excluded).
4. **Apply:**
   ```bash
   python3 <skill_dir>/scripts/migrate.py --apply
   ```
5. **Verify** (do not skip any):
   - TOML parses: `python3 -c "import tomllib;tomllib.load(open('$HOME/.sarvam/config.toml','rb'))"`
   - Every touched `SKILL.md` passes the validator:
     `python3 ~/.sarvam/skills/.system/self-knowledge/scripts/quick_validate.py <skill_dir>`
   - Memory parity: `diff -rq` source vs `~/.sarvam/memories/user/` reports no content differences
   - `memory_search` finds a known memory under the new paths
   - Hard rules present: `grep -c '^- \*\*' ~/AGENTS.md`
6. **Tell the user** to restart Sarvam Code and check `/skills` and `/mcp`.

## Judgment calls the script encodes (verify, don't blindly trust)

- **Thin prompt wrappers are not converted.** A Codex prompt that only says
  "use the $X skill" is skipped when skill X already exists — the skill already
  carries the behavior. If the wrapper adds real behavior, convert it by hand.
- **App-specific MCP servers are skipped.** Anything whose command points at
  ChatGPT/Codex app binaries (`node_repl`, `computer-use`, `*.app` paths) will
  not work in Sarvam. Name-based and path-based skip lists are in the script;
  extend them if new cases appear.
- **Secrets are never read, copied, or printed.** The script never opens
  `secrets.json`, `.credentials.json`, `auth.json`, or keyring files. MCP env
  values are written to the target config (that is where servers read them
  from) but are always redacted in plan output. Never echo them into chat.
- **Machine-generated memory extensions are excluded.** Stores like
  `chronicle/` or `skysight/` (hundreds of observed-screen logs) would pollute
  memory search. Only curated stores migrate. If the user wants everything,
  copy by hand and say so.
- **Existing Sarvam config is never clobbered.** MCP servers already present in
  `~/.sarvam/config.toml` are kept; new ones are appended. Existing skills with
  the same name are kept. Every overwrite of an existing file takes a
  timestamped `.bak-<stamp>` backup first.

## Safety rules

- Dry-run is the default. Never run `--apply` without showing the user the plan.
- Never print secret values from any source file — not in chat, not in logs,
  not in the plan output.
- Never modify files under `~/.codex/` or `~/.claude/` except the in-place
  frontmatter repair of `SKILL.md` files (which is backed up first).
- If a source config fails to parse, skip it with a message — never guess.
- The home directory is usually not a git repo; `diff_summary`-style receipts
  may be unavailable. Verify with direct checks instead and say so plainly.

## Notes

- Sarvam discovers skills from `~/.claude/skills`, `~/.codex/skills`, and
  `~/.agents/skills` natively in current builds; linking into
  `~/.sarvam/skills` guarantees discovery everywhere and survives tool removal.
- Sarvam has no custom slash-command system; commands become skills invoked as
  `$name` or by description match.
- `$ARGUMENTS` in converted commands becomes an instruction to read the
  arguments from the user's message; `AskUserQuestion` becomes
  `request_user_input`.
