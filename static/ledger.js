// Ledger shell: appearance and colour theme, ambient effects, dialogs, toast, shutdown, keyboard shortcuts.
// COLOR_THEMES and THEME_FONT_URLS are globals from base.html's head script, which also applies the saved
// theme before first paint.

function updateThemeColorMeta() {
    var bg = getComputedStyle(document.documentElement).backgroundColor;
    if (!bg) return;
    // Safari only samples theme-color on load, so a fresh tag is swapped in rather than the old one edited.
    var old = document.querySelector('meta[name="theme-color"]');
    if (old) old.remove();
    var meta = document.createElement('meta');
    meta.name = 'theme-color';
    meta.content = bg;
    document.head.appendChild(meta);
}

// Travel is a fraction of the layer itself (a column as tall as the layer, a row as wide), never vh/vw, so
// particles cross the whole window even where vh disagrees with the visible height.
function spawnFallingParticles(layer, o) {
    for (var i = 0; i < o.count; i++) {
        var col = document.createElement('span'), g = document.createElement('span');
        var d = o.durationMin + Math.random() * o.durationRange;
        col.className = 'wd-fx-col';
        g.className = 'wd-fx-glyph';
        g.textContent = o.glyphs[i % o.glyphs.length];
        col.style.left = Math.random() * 100 + '%';
        col.style.animationDuration = g.style.animationDuration = d + 's';
        col.style.animationDelay = g.style.animationDelay = -Math.random() * d + 's';
        g.style.fontSize = o.sizeMin + Math.random() * o.sizeRange + 'rem';
        g.style.opacity = (o.opacityMin + Math.random() * o.opacityRange).toFixed(2);
        g.style.setProperty('--fx-drift', Math.random() * o.driftRange - o.driftRange / 2 + 'px');
        col.appendChild(g);
        layer.appendChild(col);
    }
}

var FX_EYES = [[8, 15, 70, 60, 40, 25, 0], [85, 20, 15, 70, 60, 45, -6], [50, 75, 20, 35, 80, 65, -12], [30, 55, 65, 15, 10, 80, -18]];

// Reads both theme axes off <html>, so the mode toggle and the colour picker can each call it.
function renderThemeFx() {
    var layer = document.getElementById('theme-fx-layer');
    if (!layer) return;
    layer.innerHTML = '';
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var root = document.documentElement;
    var theme = root.getAttribute('data-color-theme') || 'slate';
    var dark = root.getAttribute('data-theme') === 'dark';
    if (theme === 'holly') {
        spawnFallingParticles(layer, { glyphs: ['❄', '❅', '❆'], count: 28, sizeMin: 0.7, sizeRange: 1.1, opacityMin: 0.45, opacityRange: 0.5, driftRange: 60, durationMin: 9, durationRange: 10 });
    } else if (theme === 'pumpkin' && !dark) {
        spawnFallingParticles(layer, { glyphs: ['🍂', '🍁'], count: 22, sizeMin: 0.9, sizeRange: 0.9, opacityMin: 0.55, opacityRange: 0.4, driftRange: 80, durationMin: 8, durationRange: 9 });
    } else if (theme === 'pumpkin') {
        FX_EYES.forEach(function(e) {
            var s = document.createElement('span');
            s.className = 'wd-fx-eyes';
            s.textContent = '👀';
            s.style.cssText = '--fx-ex1:' + e[0] + '%;--fx-ey1:' + e[1] + '%;--fx-ex2:' + e[2] + '%;--fx-ey2:' + e[3] + '%;--fx-ex3:' + e[4] + '%;--fx-ey3:' + e[5] + '%;animation-delay:' + e[6] + 's';
            layer.appendChild(s);
        });
    } else if (theme === 'bloom') {
        spawnFallingParticles(layer, { glyphs: ['🌸', '🌸', '💮'], count: 24, sizeMin: 0.7, sizeRange: 0.8, opacityMin: 0.5, opacityRange: 0.45, driftRange: 90, durationMin: 10, durationRange: 10 });
    } else if (theme === 'tide') {
        for (var c = 0; c < 8; c++) {
            var row = document.createElement('span'), g = document.createElement('span'), d = 16 + Math.random() * 14;
            row.className = 'wd-fx-row';
            g.className = 'wd-fx-glyph';
            g.textContent = '🦀';
            row.style.bottom = Math.random() * 6 + '%';
            row.style.animationDirection = c % 2 ? 'reverse' : 'normal';
            row.style.animationDuration = d + 's';
            row.style.animationDelay = -Math.random() * d + 's';
            g.style.fontSize = 1.1 + Math.random() * 0.7 + 'rem';
            g.style.opacity = (0.6 + Math.random() * 0.35).toFixed(2);
            g.style.animation = 'none';
            row.appendChild(g);
            layer.appendChild(row);
        }
    }
}

// Appearance: light, dark or system, cycled by one button.
(function() {
    var root = document.documentElement;
    var KEY = 'walkingpad-theme';
    var btn = document.getElementById('theme-toggle');
    var MODES = { light: ['Light', 'bi-sun'], dark: ['Dark', 'bi-moon'], system: ['System', 'bi-display'] };
    var mode = MODES[localStorage.getItem(KEY)] ? localStorage.getItem(KEY) : 'system';
    var system = window.matchMedia('(prefers-color-scheme: dark)');

    function apply(m, skipMeta) {
        mode = m;
        if (m === 'system') localStorage.removeItem(KEY); else localStorage.setItem(KEY, m);
        root.setAttribute('data-theme', m === 'dark' || (m === 'system' && system.matches) ? 'dark' : 'light');
        if (btn) {
            btn.setAttribute('aria-label', 'Appearance: ' + MODES[m][0]);
            btn.querySelector('.bi').className = 'bi ' + MODES[m][1];
            btn.querySelector('.lbl').textContent = MODES[m][0];
        }
        if (!skipMeta) updateThemeColorMeta();
    }
    apply(mode, true);
    if (btn) btn.addEventListener('click', function() {
        apply({ light: 'dark', dark: 'system', system: 'light' }[mode]);
        renderThemeFx();
    });
    system.addEventListener('change', function() {
        if (mode === 'system') { apply('system'); renderThemeFx(); }
    });
})();

// Colour theme: a popover menu of radio items, Standard then Special.
(function() {
    var root = document.documentElement;
    var KEY = 'walkingpad-color-theme';
    var menu = document.getElementById('color-theme-menu');
    if (!menu) return;

    function loadFonts(id) {
        var url = THEME_FONT_URLS[id];
        if (!url || document.querySelector('link[href="' + url + '"]')) return;
        var link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = url;
        document.head.appendChild(link);
    }

    function apply(id, skipMeta) {
        if (id === 'slate') root.removeAttribute('data-color-theme');
        else { root.setAttribute('data-color-theme', id); loadFonts(id); }
        menu.querySelectorAll('[data-swatch-id]').forEach(function(el) {
            el.setAttribute('aria-checked', el.getAttribute('data-swatch-id') === id ? 'true' : 'false');
        });
        if (!skipMeta) updateThemeColorMeta();
        renderThemeFx();
    }

    function group(label, themes) {
        var head = document.createElement('p');
        head.className = 'wd-menu-label';
        head.id = 'theme-group-' + label.toLowerCase();
        head.setAttribute('role', 'presentation');  // a menu may only hold groups and items; the label names its group
        head.textContent = label;
        var box = document.createElement('div');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-labelledby', head.id);
        themes.forEach(function(t) {
            var item = document.createElement('button');
            item.type = 'button';
            item.className = 'wd-theme-item';
            item.setAttribute('role', 'menuitemradio');
            item.setAttribute('data-swatch-id', t.id);
            var sw = document.createElement('span');
            sw.className = 'wd-swatch';
            sw.setAttribute('aria-hidden', 'true');
            var split = t.swatchSplit || [t.swatch, t.swatch];
            sw.style.setProperty('--a', split[0]);
            sw.style.setProperty('--b', split[1]);
            item.append(sw, t.label);
            item.addEventListener('click', function() {
                if (t.id === 'slate') localStorage.removeItem(KEY); else localStorage.setItem(KEY, t.id);
                apply(t.id);
                menu.hidePopover();
            });
            box.appendChild(item);
        });
        menu.append(head, box);
    }
    group('Standard', COLOR_THEMES.filter(function(t) { return !t.special; }));
    group('Special', COLOR_THEMES.filter(function(t) { return t.special; }));

    // Arrow keys move between items, as in any menu.
    menu.addEventListener('keydown', function(e) {
        var items = Array.prototype.slice.call(menu.querySelectorAll('[role="menuitemradio"]'));
        var i = items.indexOf(document.activeElement);
        var next = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
        if (next === undefined) return;
        e.preventDefault();
        items[(next + items.length) % items.length].focus();
    });
    menu.addEventListener('toggle', function(e) {
        if (e.newState === 'open') (menu.querySelector('[aria-checked="true"]') || menu.querySelector('button')).focus();
    });

    var saved = localStorage.getItem(KEY) || 'slate';
    if (!COLOR_THEMES.some(function(t) { return t.id === saved; })) {
        // A since-removed theme id would otherwise leave nothing checked and no palette applied.
        localStorage.removeItem(KEY);
        saved = 'slate';
    }
    apply(saved, true);
})();

// Native dialogs: Esc closes (built in); a click on the backdrop closes too; focus returns to the opener.
var dialogOpener = null;
function openDialog(id) {
    var d = document.getElementById(id);
    if (!d || d.open) return;
    dialogOpener = document.activeElement;
    d.showModal();
}
document.addEventListener('click', function(e) {
    var d = e.target;
    if (d.tagName !== 'DIALOG' || !d.open) return;
    var r = d.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close();
});
document.addEventListener('close', function(e) {
    if (e.target.tagName === 'DIALOG' && dialogOpener && dialogOpener.isConnected) dialogOpener.focus();
    dialogOpener = null;
}, true);

// Themed replacement for confirm(): resolves true only when the confirm button is pressed.
function confirmDialog(title, message, confirmLabel) {
    var d = document.getElementById('confirm-dialog');
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-message').textContent = message;
    document.getElementById('confirm-ok').textContent = confirmLabel;
    d.returnValue = '';
    openDialog('confirm-dialog');
    return new Promise(function(resolve) {
        d.addEventListener('close', function() { resolve(d.returnValue === 'ok'); }, { once: true });
    });
}

// [data-tip] tooltips show on hover and on focus (a bare title attribute doesn't show on focus), in one fixed element
// so a scrolling container can't clip them. Read at show time, so SSE updates to data-tip apply.
(function() {
    var tip = document.createElement('div');
    tip.id = 'tip';
    tip.className = 'wd-tip';
    tip.setAttribute('aria-hidden', 'true');
    tip.hidden = true;
    document.body.appendChild(tip);
    var owner = null;
    function show(el) {
        owner = el;
        tip.textContent = el.getAttribute('data-tip');
        tip.hidden = false;
        var r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
        var top = r.top - t.height - 6;
        tip.style.left = Math.min(Math.max(8, r.left + r.width / 2 - t.width / 2), innerWidth - t.width - 8) + 'px';
        tip.style.top = (top < 8 ? r.bottom + 6 : top) + 'px';
        tip.classList.add('is-on');
    }
    function hide() { owner = null; tip.classList.remove('is-on'); tip.hidden = true; }
    document.addEventListener('pointerover', function(e) { var el = e.target.closest('[data-tip]'); if (el && el !== owner) show(el); });
    document.addEventListener('pointerout', function(e) { if (owner && !owner.contains(e.relatedTarget) && document.activeElement !== owner) hide(); });
    document.addEventListener('focusin', function(e) { var el = e.target.closest('[data-tip]'); if (el) show(el); else if (owner) hide(); });
    document.addEventListener('focusout', function(e) { if (e.target === owner) hide(); });
    window.addEventListener('scroll', function() { if (owner) hide(); }, true);
})();

var toastTimer;
function toast(message) {
    var t = document.getElementById('toast');
    if (!t) return;
    t.textContent = message;
    t.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function() { t.classList.remove('is-on'); }, 3000);
}
(function() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('saved') === '1') {
        history.replaceState({}, '', window.location.pathname);
        toast('Settings saved');
    }
})();

function showShutdownOverlay() {
    shutdownShown = true;
    document.title = 'WalkingDad';
    document.body.innerHTML = '<div class="wd-shutdown" role="status"><div><h1>WalkingDad is shutting down</h1>'
        + '<p>It stops the belt and disconnects the WalkingPad. You can close this tab.</p></div></div>';
}

(function() {
    var confirmBtn = document.getElementById('shutdown-confirm');
    if (!confirmBtn) return;
    document.getElementById('shutdown-button').addEventListener('click', function() { openDialog('shutdown-dialog'); });
    confirmBtn.addEventListener('click', function() {
        fetch(confirmBtn.getAttribute('data-url'), { method: 'POST' });
        showShutdownOverlay();
        // Gives the request a moment to leave before the tab closes.
        setTimeout(function() { window.close(); }, 750);
    });
})();

// Each shortcut clicks its button (tagged via hotkey() in app.py), so it goes through the same form submit,
// double-tap guard and origin check.
(function() {
    var help = document.getElementById('shortcuts-dialog');
    if (!help) return;
    function bound(key) {
        return document.querySelector('button[aria-keyshortcuts~="' + CSS.escape(key) + '"]');
    }
    var KEY_GLYPHS = { ArrowUp: '↑', ArrowDown: '↓' };
    function keyNames(b) {
        return b.getAttribute('aria-keyshortcuts').split(' ').map(function(k) { return KEY_GLYPHS[k] || k; });
    }
    var tagged = document.querySelectorAll('button[aria-keyshortcuts]');
    tagged.forEach(function(b) { b.title = 'Shortcut: ' + keyNames(b).join(' or '); });

    // Built from this screen's tagged buttons, so the list can't drift from the real bindings.
    function showHelp() {
        if (!help.isConnected) return;  // body replaced by the shutdown overlay
        var list = document.getElementById('shortcuts-list');
        list.replaceChildren();
        tagged.forEach(function(b) {
            var row = document.createElement('div');
            var dt = document.createElement('dt'), dd = document.createElement('dd');
            keyNames(b).forEach(function(k, i) {
                if (i) dt.append(' or ');
                dt.appendChild(document.createElement('kbd')).textContent = k;
            });
            var name = b.cloneNode(true);
            name.querySelectorAll('kbd').forEach(function(k) { k.remove(); });
            dd.textContent = b.getAttribute('aria-label') || name.textContent.trim();
            row.append(dt, dd);
            list.appendChild(row);
        });
        document.getElementById('shortcuts-none').hidden = tagged.length > 0;
        openDialog('shortcuts-dialog');
    }
    document.getElementById('shortcuts-button').addEventListener('click', showHelp);

    // Space on a button or link focused from the keyboard keeps its native meaning, but not after a mouse click
    // (which focuses buttons in Chrome/Firefox, not Safari). Tracked by the last input rather than :focus-visible,
    // which Chrome starts matching on a mouse-focused button once any key is pressed. Script focus counts as
    // whichever input caused it, so intent is set in the capture phase, ahead of handlers that move focus.
    // Starts true: Tabbing in from the address bar sends no keydown here.
    var keyboardIntent = true, keyboardFocused = null;
    document.addEventListener('pointerdown', function() {
        keyboardIntent = false; keyboardFocused = null; document.documentElement.dataset.intent = 'pointer';
    }, true);
    document.addEventListener('keydown', function() {
        keyboardIntent = true; document.documentElement.dataset.intent = 'keyboard';
    }, true);
    document.addEventListener('focusin', function(e) { keyboardFocused = keyboardIntent ? e.target : null; });

    document.addEventListener('keydown', function(e) {
        // Held keys don't repeat: each press is a locked BLE command.
        if (e.defaultPrevented || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.target.closest('input, select, textarea, [contenteditable]')) return;
        if (e.key === ' ' && e.target === keyboardFocused && e.target.closest('button, a, summary, [role="button"]')) return;
        if (document.querySelector('dialog[open], :popover-open')) return;
        if (e.key === '?') {
            e.preventDefault();
            showHelp();
            return;
        }
        var btn = bound(e.key === ' ' ? 'Space' : (e.key.length === 1 ? e.key.toUpperCase() : e.key));
        if (!btn) return;
        // Swallowed even while disabled (belt transitioning), so Space and the arrows don't scroll the page.
        e.preventDefault();
        if (!btn.disabled) btn.click();
    });
})();
