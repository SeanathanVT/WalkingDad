"""Exit non-zero unless the PR/MR title given as argv[1] follows Conventional Commits.

Shared by GitHub Actions and GitLab CI so both platforms enforce the same rule.
"""

import re
import sys

TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
PATTERN = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([^()\s]+\))?!?: \S")
# GitLab puts the draft marker in the title itself; GitHub keeps it separate.
DRAFT_PREFIX = re.compile(r"^(?:\[draft\]|\(draft\)|draft:)\s*", re.IGNORECASE)


def is_valid(title: str) -> bool:
    return bool(PATTERN.match(DRAFT_PREFIX.sub("", title.strip())))


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else ""
    if not is_valid(title):
        sys.exit(
            f"PR title {title!r} doesn't follow Conventional Commits: `type(scope): summary`, "
            f"type one of {', '.join(TYPES)}. Squash merges make the title the commit subject."
        )
    print(f"PR title OK: {title}")
