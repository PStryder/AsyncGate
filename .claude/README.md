# .claude/ Directory Convention

**Purpose:** AI workspace for session continuity and working files

Files here are **point-in-time session artifacts**, not project documentation.
They record what was true when written and are not maintained afterwards, so
file paths, module names and line numbers in them may no longer resolve, and
findings may have been fixed or superseded since. For current guidance read
`README.md`, `docs/`, and the canonical contracts in
`LegiVellum/docs/canonical/`.

## What goes here:

- **Session notes** (SESSION_SUMMARY.md, etc.)
- **Progress tracking** (REALIGNMENT_PROGRESS.md, etc.)
- **AI collaboration notes** (HEXY_REFINEMENTS.md, corrections, clarifications)
- **Temporary working files** (commit messages, scratch notes)
- **Permission settings** (settings.local.json - Claude Code/Desktop)

## What does NOT go here:

- Production code (`src/`, `tests/`)
- Project documentation (`docs/`, `README.md`)
- Specifications (`AsyncGate Spec.txt`)
- Build/deploy configs (`pyproject.toml`, `Dockerfile`, `alembic/`)

## Convention rationale:

**Keep the project tree clean** while preserving AI context across sessions.

Files in `.claude/` can be:
- Excluded from git (optional - add to .gitignore)
- Lost without breaking the project
- Useful for AI session handoffs and continuity

**For future AI sessions:**
- Read files in `.claude/` first to understand project state
- Store your working notes and progress tracking here
- Don't clutter the root directory with temporary AI artifacts

**Project owner preference:** Non-code AI-generated files that can be lost/not pushed belong here.

---
Last updated: 2026-01-05 (Tier 2 completion)
