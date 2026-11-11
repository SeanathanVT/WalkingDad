import pytest
from check_pr_title import is_valid


@pytest.mark.parametrize(("title", "valid"), [
    ("fix(ble): register the disconnect callback with Bleak 3.x (ROADMAP 1.7)", True),
    ("ci: check PR titles follow Conventional Commits", True),
    ("feat!: drop Python 3.9", True),
    ("refactor(ui)!: rename themes", True),
    ("Draft: docs: fix typo", True),
    ("[Draft] docs: fix typo", True),
    ("Ledger interface redesign (ROADMAP 3.21)", False),
    ("feature: add export", False),
    ("fix:no space", False),
    ("fix(): empty scope", False),
    ("fix: ", False),
    ("", False),
])
def test_is_valid(title, valid):
    assert is_valid(title) is valid
