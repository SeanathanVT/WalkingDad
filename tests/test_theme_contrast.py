"""Every colour theme, light and dark, meets WCAG 2.2 AA for the pairs the UI actually paints.

Reads the tokens straight from static/ledger.css (Slate) and static/themes.css, so a palette edit that breaks contrast
fails here rather than in someone's browser.
"""
import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
TOKENS = ("ground", "surface", "raised", "border", "control", "text", "muted", "primary", "primary-hover", "on-primary", "hue")


HEX = r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})"


def _hex6(c):
    return "#" + "".join(ch * 2 for ch in c[1:]) if len(c) == 4 else c


def _pair(value):
    value = value.strip()
    m = re.fullmatch(rf"light-dark\(({HEX}),\s*({HEX})\)", value)
    if m:
        return _hex6(m.group(1)), _hex6(m.group(2))
    if re.fullmatch(HEX, value):
        return _hex6(value), _hex6(value)
    return None


def _block(body):
    out = {}
    for name, value in re.findall(r"--([a-z-]+):\s*([^;]+);", body):
        pair = _pair(value)
        if pair:
            out[name] = pair
    return out


def _themes():
    slate = _block(re.search(r"^:root \{(.*?)^\}", (STATIC / "ledger.css").read_text(), re.DOTALL | re.MULTILINE).group(1))
    themes = {"slate": slate}
    for tid, body in re.findall(r':root\[data-color-theme="([a-z]+)"\] \{(.*?)\}', (STATIC / "themes.css").read_text(), re.DOTALL):
        tokens = _block(body)
        if "primary" in tokens:
            themes[tid] = {**slate, **tokens}
    return themes


def _lum(hex_color):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _ratio(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


THEMES = _themes()
# (foreground, background, minimum, what it is)
PAIRS = [
    ("text", "surface", 4.5, "body text on a panel"),
    ("muted", "surface", 4.5, "grey text on a panel"),
    ("muted", "raised", 4.5, "grey text on a raised panel"),
    ("muted", "ground", 4.5, "grey text on the page"),
    ("on-primary", "primary", 4.5, "primary button label"),
    ("on-primary", "primary-hover", 4.5, "primary button label on hover"),
    ("hue", "surface", 3, "data hue on a panel"),
    ("hue", "raised", 3, "belt-ladder fill on its track"),
    ("control", "surface", 3, "control border on a panel"),
    ("control", "raised", 3, "control border on a raised panel"),
]
# VT's maroon may be shaded but never lightened (brand.vt.edu), so its dark-mode selected stop gets an orange ring
# instead (themes.css); every other theme's primary must stand out from the panel on its own.
PRIMARY_ON_SURFACE_EXEMPT = {"vt"}


def test_every_picker_theme_has_a_palette():
    assert set(THEMES) == {"slate", "red", "amber", "lime", "forest", "teal", "cyan", "blue", "violet", "pink",
                           "vt", "bloom", "tide", "pumpkin", "holly"}
    for tid, tokens in THEMES.items():
        assert set(TOKENS) <= set(tokens), (tid, set(TOKENS) - set(tokens))


@pytest.mark.parametrize("tid", sorted(THEMES))
@pytest.mark.parametrize("mode", [0, 1], ids=["light", "dark"])
def test_theme_contrast(tid, mode):
    t = THEMES[tid]
    failures = [f"{what}: {t[fg][mode]} on {t[bg][mode]} = {_ratio(t[fg][mode], t[bg][mode]):.2f} < {need}"
                for fg, bg, need, what in PAIRS if _ratio(t[fg][mode], t[bg][mode]) < need]
    if tid not in PRIMARY_ON_SURFACE_EXEMPT and _ratio(t["primary"][mode], t["surface"][mode]) < 3:
        failures.append(f"primary (selected stop, buttons) on a panel: {_ratio(t['primary'][mode], t['surface'][mode]):.2f} < 3")
    assert not failures, failures


def test_vt_dark_selected_stop_has_a_ring():
    css = (STATIC / "themes.css").read_text()
    assert re.search(r':root\[data-color-theme="vt"\]\[data-theme="dark"\] \.stop\[aria-pressed="true"\] \{[^}]*box-shadow: 0 0 0 2px #e5751f', css)
