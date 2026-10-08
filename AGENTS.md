# AGENTS.md

## Docs
Keep all docs (ROADMAP.md, CHANGELOG.md `[Unreleased]`, README.md, docs/) in sync with the code and with any plans discussed, in the same change. Released CHANGELOG sections are immutable.

## Git
- Never commit. The maintainer signs commits, and agent commits come out unsigned. Hand over the commit message and list the files to add. Messages follow [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, e.g. `fix(ui): show transition hint only during belt sequences (ROADMAP 3.18)`, ending with a `Co-Authored-By:` trailer naming the agent.
- Don't stage unless asked in that turn, and then `git add` specific paths only (never `docs/` plan files). The maintainer reviews `git diff` first.
- Run `git status`/`git log` before any claim about commit state; the maintainer commits between turns, so earlier snapshots go stale.
- Feature branches merge into `development`, not `main`. Open PRs with `--base development` and scope diffs/logs against `origin/development`.

## Platforms
The maintainer runs macOS (primary) and Fedora Linux, never Windows. Suggest manual checks on macOS first, Fedora when Linux behavior differs. Windows-only code (`start_app.bat`, `WSAE*` errnos, Windows socket semantics) can't be verified by the maintainer; flag it rather than asking for a test.

## Design target
The primary client is a laptop browser at a desk above the treadmill. Design for desktop width and mouse/keyboard first; keep the phone layout working, but it doesn't drive layout or feature decisions.
