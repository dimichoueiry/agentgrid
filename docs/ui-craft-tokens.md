# Tokens — a validated default

The method in SKILL.md tells you to build one neutral ramp and one desaturated
accent. This file is a **specific, shipped answer** to that — the palette from a
real tool (a fleet dashboard: columns of cards, a side panel, a notes pad, plus a
curses front end over the same data). It survives the squint test in both light
and dark.

Use it as a starting point, not a law. **Swap the accent hue for the brand's and
keep the structure** — the structure is what does the work.

---

## Web

```css
:root{
  --bg:#fbfbfa; --card:#ffffff; --card-hover:#f4f4f2;
  --text:#18181b; --text-2:#6b6b73; --text-3:#a3a3ab;
  --line:#e7e7e4; --line-2:#d6d6d2;
  --accent:#b4690e;                  /* desaturated on purpose */
  --failed:#ef5f63;
  --shadow:0 1px 2px rgba(0,0,0,.04);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#131315; --card:#1a1a1d; --card-hover:#212126;
    --text:#ececee; --text-2:#9797a0; --text-3:#63636b;
    --line:#252529; --line-2:#33333a;
    --accent:#d9a441;
    --shadow:none;                   /* a shadow on a dark surface is mud */
  }
}
```

That is the entire palette: **two surfaces, three text greys, two hairlines, one
accent, one error red.** Eleven values. If you find yourself adding a twelfth,
check whether an existing one plus weight or opacity would do.

Notes on each group:

- **`--bg` is off-white (#fbfbfa), not #fff**, and `--card` is pure white. The
  card is *lighter* than the page, so a raised surface reads as raised without a
  border or a shadow. In dark mode the same relationship inverts naturally
  (`#131315` page, `#1a1a1d` card).
- **Three text greys is the minimum that supports a hierarchy**: primary, the
  secondary line under it, and the tertiary that must not compete (counts,
  timestamps, placeholders). A fourth grey is almost always a sign that something
  should have been a different weight instead.
- **Two hairlines**: `--line` separates (grid gaps, rules), `--line-2` outlines
  (input borders, chips). Never use `--text-3` for a border — it is too dark and
  the border starts reading as content.
- **`--shadow` is `none` in dark mode.** Elevation in dark UI comes from surface
  lightness, not from shadow.

### Where the accent is allowed

This is the part people get wrong. The accent appears in exactly these places
and nowhere else:

| Use | Why it earns it |
|---|---|
| The count of things needing a human | the one number you are looking for |
| A 2px left stub on those items | findable, not shouting |
| A due-today / overdue chip | a fact has become a prompt |
| An open-work count on a tab | outstanding work, seen without navigating |
| The selected day in a repeat picker | the answer to "when does this come back" |
| A tick beside the current choice in a menu | which one is active |
| A primary button outline, and a live rename field's border | where you are typing |

Everything else — tags, labels, statuses, categories, group names — stays
neutral. **Colouring a tag competes with the one accent that means "act on
this",** and once two things are amber neither is urgent.

A second hue is justified **only when telling two things apart is itself the
information.** The one case that qualified was speaker identity in a transcript:

```css
--you:#b4690e; --claude:#2f6ba8;      /* light */
--you:#d9a441; --claude:#5b9dd9;      /* dark  */
```

Both muted to the same degree as the accent, and defined as their own tokens
rather than reusing `--accent`, so "a human is needed" stays a separate idea from
"this is who is speaking."

### Type

```css
body { font: 13.5px/1.5 var(--sans); -webkit-font-smoothing: antialiased }
.pad { font: 13.5px/1.7 var(--mono) }     /* writing surface: looser leading */
```

System fonts only — no web font, no CDN. The scale, and it is deliberately
narrow:

| Size | Used for |
|---|---|
| 15px | panel and sheet titles |
| 13.5px | body |
| 13px | item names |
| 12.5px | column headings, buttons |
| 11.5px | secondary lines, field labels |
| 11px | ages, hints |
| 10.5px | tool traces, metadata |
| 10px | tags, chips |

Weights: 600 for titles, 550 for item names, 620 for an unread item, 400
everywhere else. **Nothing is bold for emphasis** — bold is structural.

Letter-spacing: `-.01em` on names, `-.015em` on titles. Only tighten, never
loosen, except on 10px uppercase micro-labels (`.4px`) where the skill's
anti-pattern list applies anyway — use them once, for speaker labels, or not at
all.

**`font-variant-numeric: tabular-nums` on every number that changes** — ages,
counts, page tallies. Without it the digits jitter on each poll and the eye
tracks the motion instead of the value.

### Shape and motion

```css
/* radii */  5–6px controls · 6px cards · 7px messages · 8–10px popovers/sheets
/* transitions */
.12s            hover, press, opacity reveals
.18–.2s         panels, scrims
260ms cubic-bezier(.32,.72,0,1)    an item travelling between columns (FLIP)
```

**One easing curve, reused.** `cubic-bezier(.32,.72,0,1)` is fast-out,
settle-in — it reads as physical rather than as an effect. Using a second curve
elsewhere makes the two look like bugs.

The only looping animation in the whole app:

```css
@keyframes breathe { 0%,100%{opacity:.35} 50%{opacity:1} }   /* 1.8s, running items only */
```

### Six patterns that carry the look

```css
/* 1. Hairlines from grid gaps — no borders on the children at all */
.board{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
       gap:1px;background:var(--line)}
.col{background:var(--bg)}

/* 2. The accent as a 2px stub: enough to find, not enough to shout */
.card.needs::before{content:"";position:absolute;left:0;top:10px;bottom:10px;
  width:2px;border-radius:2px;background:var(--accent)}

/* 3. Selection as an INSET outline — no layout shift */
.card.sel{outline:1.5px solid var(--text-3);outline-offset:-1.5px}

/* 4. Drop targets outlined, not filled, so their contents stay readable */
.col-body.over{outline:1.5px dashed var(--line-2);outline-offset:-6px;border-radius:8px}

/* 5. Toggles: off is a quiet outline, on is filled. aria-pressed IS the hook,
      so visual state and a11y state cannot drift apart. */
button[aria-pressed=true]{background:var(--card);color:var(--text)}

/* 6. Controls revealed on hover — include :focus-within for keyboard users */
.acts{opacity:0;transition:opacity .12s}
.row:hover .acts,.row.open .acts,.row:focus-within .acts{opacity:1}
```

Panels slide with `transform`, never width or `right`:

```css
.panel{transform:translateX(100%);transition:transform .2s cubic-bezier(.32,.72,0,1)}
.panel.on{transform:none}
```

### Two traps that cost real time

**An undeclared custom property fails silently.** `var(--mono)` with no
declaration is invalid at computed-value time, the inherited value wins, and
there is no console warning. In the source this file was extracted from, nine
monospace rules silently rendered in the body sans font for exactly that reason.
Either declare the token or give every `var()` an inline fallback —
`var(--failed,#ef5f63)` — but never neither.

**A CSS unicode escape has no `u`.** It is `content:"\203A"`. The JS/JSON
spelling is read by CSS as an escaped letter `u` followed by the digits, so the
glyph renders as the literal text `u203A`.

---

## Terminal (curses)

The same discipline: hues mean "alive and its state matters", everything
structural is a neutral grey. xterm-256 indices, chosen so the three greys are
visibly separated on a dark background and the status hues share a similar
perceived lightness — otherwise one card looks louder purely because of its hue.

```python
BLOCKED = 214   # amber — needs a human
WORKING = 45    # cyan  — running
DONE    = 71    # muted green, deliberately not the brightest green
FAILED  = 203   # soft red
IDLE    = 244   # grey: the least important state gets no hue at all
HEADER  = 141   # violet — the wordmark ONLY
TITLE   = 253   # ── the neutral ramp: three greys, nothing else structural
MUTED   = 245
FRAME   = 237
DIM     = 240
EDGE_BLOCKED = 136   # status hue at frame weight, for a tinted outline
EDGE_WORKING = 31
```

8-colour fallback: map the hues to `YELLOW/CYAN/GREEN/RED/MAGENTA`; set idle,
title and muted to `-1` (default foreground) so idle still reads as
unremarkable; frame and dim to `BLACK`.

Rules that matter more than the numbers:

- **Never map a "dim" role to a saturated colour.** Doing so puts chroma on
  every border and subtitle on screen and leaves status nothing to stand out
  against.
- **Filled chips (`init_pair(pair, BLACK, hue)`) only for states a human must act
  on.** Reserved, so they keep their urgency.
- **Selection is line weight, not reverse video.** A reversed pair across a whole
  border reads as a solid block and fights the status colours:
  ```
  light  ╭ ╮ ╰ ╯ ─ │        heavy  ┏ ┓ ┗ ┛ ━ ┃
  ```
- **Tier titles by liveness:** bright bold for live work, one grey down for
  settled. A card that finished 15 days ago must not compete with one running now.
- **Two-tone the footer** — key bright, label dim, and spell the modifier
  (`⇧R`). A single-colour `R rename` in a row of lowercase keys reads as
  "press r".
- Probe colour support defensively; `curses` is often mocked in tests, where
  comparisons on it raise:
  ```python
  try: rich = int(curses.COLORS) >= 256
  except (TypeError, ValueError, AttributeError): rich = False
  ```

---

## Re-hueing for another brand

1. Replace `--accent` in both schemes with the brand colour, **desaturated** —
   if it is a vivid brand hue, mute it until it stops competing with text. The
   light and dark values are different: dark mode needs a lighter, warmer accent
   (`#b4690e` → `#d9a441`) to hold up against a dark surface.
2. Leave the neutral ramp alone unless the brand is genuinely warm or cool, in
   which case shift all six greys by the same small hue amount. Keep the
   lightness steps.
3. Do not add hues. If the brand has five, pick the one that means "act on
   this" and keep the rest for marketing.
4. Re-run the squint test in both schemes. Whatever survives should be the thing
   that needs a human.
