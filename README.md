# migrate-to-sarvam

[![test](https://github.com/udayanwalvekar/migrate-to-sarvam/actions/workflows/test.yml/badge.svg)](https://github.com/udayanwalvekar/migrate-to-sarvam/actions/workflows/test.yml)

An [agent skill](https://skills.sh) that migrates your **Claude Code** and
**Codex** setup into **Sarvam Code**: skills, slash commands, MCP servers,
durable memories, and global instructions — with a dry-run plan you review
before anything is written.

## What it migrates

| Source | Destination | How |
|--------|-------------|-----|
| `~/.claude/skills/`, `~/.codex/skills/`, `~/.agents/skills/` | discovered in place + linked into `~/.sarvam/skills/` | symlink (or copy with `--copy-mode`) |
| `SKILL.md` files missing frontmatter | same file, frontmatter prepended | in-place edit (backed up first) |
| `~/.claude/commands/*.md`, `~/.codex/commands/*.md`, `~/.codex/prompts/*.md` | `~/.sarvam/skills/<name>/SKILL.md` | converted to skills |
| Claude `settings.json` `mcpServers` + Codex `config.toml` `mcp_servers` | `~/.sarvam/config.toml` | TOML append, never clobbers |
| Curated memory stores | `~/.sarvam/memories/user/` | file copy, byte-identical |
| `~/AGENTS.md` | same file | seeded or appended with hard rules + memory pointers |

**Safety model:** dry-run by default, timestamped backups before every
overwrite, secrets never read or printed (MCP env values are redacted in plan
output), app-specific servers (ChatGPT/Codex binaries) and machine-generated
memory stores are excluded, and re-running on a migrated home is a no-op.

## Install

Works with any agent that loads `SKILL.md` skills (Claude Code, Codex,
Sarvam Code, and other compatible CLIs).

**With the skills CLI:**

```bash
npx skills add udayanwalvekar/migrate-to-sarvam@migrate-to-sarvam
```

**Or clone and copy** into your tool's skills directory:

```bash
git clone https://github.com/udayanwalvekar/migrate-to-sarvam
mkdir -p ~/.sarvam/skills   # or ~/.claude/skills / ~/.codex/skills
cp -R migrate-to-sarvam/skills/migrate-to-sarvam ~/.sarvam/skills/
```

## Run

Ask your agent to **migrate my Claude Code and Codex setup to Sarvam Code** —
it will load the skill, survey your home directory, show you a dry-run plan,
and only apply it after you confirm. Or run the script directly:

```bash
python3 ~/.sarvam/skills/migrate-to-sarvam/scripts/migrate.py            # dry run
python3 ~/.sarvam/skills/migrate-to-sarvam/scripts/migrate.py --apply    # execute
python3 ~/.sarvam/skills/migrate-to-sarvam/scripts/migrate.py --copy-mode --apply  # copy skills instead of symlinking
```

Requires Python 3.9+ (stdlib only). Python 3.11+ recommended for TOML support;
on older interpreters install `tomli` to migrate Codex MCP servers.

## Verify

After applying, restart your Sarvam Code session and check:

- `/skills` — migrated skills appear
- `/mcp` — migrated MCP servers connect
- `memory_search` / memory tools — curated memories are findable

## License

[MIT](LICENSE)
