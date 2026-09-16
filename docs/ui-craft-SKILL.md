---
name: ui-craft
description: Design and build interfaces that look considered rather than generated - dashboards, TUIs, internal tools, admin panels, any screen with state on it. Use when asked to build, restyle, critique or "make nice" a UI; when the user says a design looks bad, cluttered, generic, "AI-slop"/"AI-pilled", or asks you to "surprise me" with a design; and before writing the first line of CSS, curses drawing code, or component markup for anything a human will look at. Covers the palette method, hierarchy, decluttering, motion, keyboard and empty states, plus the verification pass that catches the bugs UI code actually ships with. A validated default palette with concrete values - both colour schemes, type scale, radii, motion, and a curses ramp - is documented in `references/tokens.md`; swap that file's accent for your brand's.
---

# UI craft

Build interfaces someone with taste would ship. The default failure is not
ugliness — it is *genericness*: a screen that looks like every other generated
dashboard, because every element was styled independently and nothing was
subtracted.

## The one rule everything else follows from

**Find what already carries the meaning, then stop repeating it.**

Most bad UI says the same thing three times. A card in a "Needs you" column,
with an amber border, an amber badge reading NEEDS YOU, and a pulsing dot, has
told you once and shouted twice. Ink spent restating is ink unavailable for
anything else, and the screen reads as noise.

Ask: what dimension is *already* encoding this?

| If this carries it | Then this becomes free |
|---|---|
| Position (column, order, grouping) | colour, badges |
| Type weight and size | borders, boxes |
| Whitespace | dividers, cards |
| The one accent | every other hue |

The strongest version of this: **once state lives in columns, colour is no
longer needed for state at all.** That single move can take a screen from five
competing hues to near-monochrome, and it is usually the difference between
"generated" and "designed".

## The palette method

1. **One neutral ramp** — background, surface, three text greys, one hairline.
   Everything structural is drawn from this and nothing else.
2. **One accent**, desaturated. Reserve it for the single thing a human must
   act on. If the accent appears in three unrelated places, it means nothing.
3. **Semantic hues only where the semantics are real** — error red, and maybe
   a running/active tint. Not one hue per category.
4. **The least important state gets no hue.** Idle, done, archived: pure grey,
   optionally dimmed. Calm is a feature when 15 of 22 rows are idle.

Test: squint at the screen. Whatever you still see should be the thing that
needs you. If everything survives the squint, the palette is wrong.

**For the concrete values, read `references/tokens.md`** — a validated default
palette (both colour schemes, the type scale, radii, the one easing curve, the
curses 256/8-colour ramp) with a note on where the accent is allowed to appear
and how to re-hue it for another brand. Start from that rather than inventing
eleven values, and swap the accent for the brand's.

For terminals: prefer 256-colour with an 8-colour fallback, and probe
defensively — `curses` is often mocked in tests, so comparisons on it can raise.
Never map a "dim" role to a saturated colour; that one mistake can put chroma on
90% of the ink.

## Hierarchy without chrome

- **Tier titles by liveness.** Live work bright and bold; settled work one grey
  down. A card that finished 15 days ago must not compete with one running now.
- **Dim whole rows**, not just their text, for things that are done with.
  `opacity: .62`, restored on hover.
- **Right-align the number people compare** (age, count, size), and use
  `font-variant-numeric: tabular-nums` so digits line up.
- **Borders are a last resort.** Whitespace groups; a single hairline separates;
  an outline is for a cursor or a focus ring.
- **No shadows, no gradients, no hover lift** unless depth is genuinely being
  communicated.

## Decluttering: the two-line test

Before adding a field to a repeating element, ask what it displaces. A card
carrying title, badge, age, project, branch, kind, two lines of prompt, four
tool chips and three sub-rows is unreadable at a glance — which is the only way
a card is ever read.

**Cap a repeating item at two lines: what it is, and where/when.** Everything
else moves one click away into a detail panel. If that feels like hiding
information, that is the point: a list is for scanning, a panel is for reading.

Counts beat lists. `7 agents, 2 running` on the card; the roster in the panel.

## Motion

Animate only what carries information.

- **Yes:** an item physically moving between columns when its state changes.
  Use FLIP — measure, move, then play the difference backwards — so the change
  is legible rather than a jump.
- **No:** pulses, glows, hover lifts, spinners on things that are not spinning,
  anything that loops forever competing for attention.
- One slow pulse (2.5s+) on genuinely urgent items is the maximum. Fast blinking
  is something people learn to ignore.
- Patch the DOM in place on refresh. Re-rendering wholesale every poll drops
  hover state, kills animations mid-cycle, and destroys text selection.

## Controls and affordances

- **Label controls with verbs, not nouns.** A collapse button labelled with the
  same noun as the panel's heading reads as a title, not an action — the user
  will not find it.
- **One control per direction, each where you would look for it.** A `›`
  chevron inside a panel closes it; a labelled button where the panel used to be
  reopens it. Never two controls for the same thing on screen at once.
- **Hiding must not hide the fact.** A collapsed panel keeps its count on the
  toggle: `‹ Todos 3`. Collapsing removes the list, not the knowledge.
- **Distinguish key from label in shortcut hints.** `⇧R rename` in a row of
  lowercase keys reads as "press r". Two-tone it, and spell the modifier.
- **Guard single-key shortcuts behind an is-typing check** covering `input`,
  `textarea`, `select` and contenteditable — or `/` will fire while someone is
  typing a path.
- **Persist view preferences** (collapsed panels, hidden groups, chosen filter)
  to localStorage. Re-hiding the same thing every session is a papercut.

## States you will otherwise forget

- **Empty:** say what to do, not that there is nothing.
  "Write `- [ ] something` in the pad", not "No items".
- **Loading:** never a layout jump. Reserve the space.
- **Error:** state the cause and the fix in the same sentence.
- **Truncated:** if a view caps at N, say so. A silent cap reads as completeness.
- **Stale:** show when data last refreshed if it polls.

## Density and layout

- Repeating items: `minmax()` grid, not fixed columns, and let content set the
  breakpoint.
- Give the reading surface the width; give the index the sidebar.
- Respect `prefers-color-scheme` and design both. A tool that ignores the system
  looks like a web page, not an application.
- Right-to-left safety and `min-width: 0` on flex children that hold ellipsised
  text — otherwise they refuse to shrink.

## "Surprise me"

When asked to surprise, do **not** free-associate. Follow this:

1. **Name what makes the current version look generated.** Be specific and
   itemised — violet accent, glow-pulse, gradient fill, uppercase micro-labels,
   five hues, shadows. The user usually cannot articulate it; naming it is most
   of the value and it proves the redesign is diagnosis-led.
2. **Find the one structural move** that removes a whole category of decoration
   at once, rather than restyling element by element. Columns removing the need
   for colour. Two-line cards removing the need for a scroll. One accent
   removing the need for a legend.
3. **State the principle before the pixels.** "Position carries state, so colour
   does not" is the deliverable; the CSS is its consequence.
4. **Show it rendered**, not described. For a TUI, run the real drawing code
   through a fake screen and paste the character grid. For HTML, produce the
   page and describe what changed structurally.
5. **Leave the reasoning in the code.** A header comment listing the design
   rules stops the next change from undoing them by accident.

The surprise should come from restraint and from a structural insight — not
from novelty. Nobody is delighted by a new gradient.

## Verify before claiming it works

UI code fails silently. Run these every time:

- **Syntax:** extract inline `<script>` and `node --check` it. Parse any Python
  that changed.
- **Import every module** after adding a call to a new one — testing a helper in
  isolation will not catch a missing import at the call site.
- **Tag balance:** count `<tag[\s>]` against `</tag>` — and use `[\s>]`, not
  `[ >]`, or a tag split across a newline gives a false mismatch.
- **No stray control characters.** A literal NUL in a source file makes it
  "binary" to grep, diff and `file(1)`, and every later search silently returns
  nothing. If you need a sentinel, write it as an escape (the six characters `\uE000`), never as a
  raw byte.
- **Render with real data**, including the pathological rows: the longest title,
  the one with 40 children, the empty one.
- **Re-read state you read more than a minute ago** before acting on it.

### The specificity bug that will bite you

```css
.panel { display: grid; }     /* (0,1,0) */
/* browser default: [hidden] { display:none } is (0,0,1) — it loses */
```

Setting `.hidden = true` in JS then does **nothing**, and the element stays on
screen. Any element whose `display` comes from a class needs an explicit
`.panel[hidden]{display:none}`. Add it when you write the class, not after
shipping the bug twice.

## Anti-patterns

The tells that make a UI read as generated:

- A different hue per category, with a legend to decode it
- Uppercase micro-labels everywhere
- Gradient fills and glow shadows on cards
- Badges restating what position already says
- Hover lift on every element
- Emoji as iconography
- Every value the same weight, so nothing is findable
- Pie charts, and any chart where a sorted bar list would do
- A spinner where the previous value could have stayed on screen
