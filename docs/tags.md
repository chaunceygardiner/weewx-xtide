---
title: Tags in your skin
layout: default
nav_order: 4
description: $xtide.events() — upcoming high/low tides as data with WeeWX unit formatting — and $xtide.graph() for embedding the interactive tide graph in your own skin.
---

# Tags in your skin

[weewx-xtide manual](https://chaunceygardiner.github.io/weewx-xtide/) ·
[weewx-xtide on GitHub](https://github.com/chaunceygardiner/weewx-xtide) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-xtide/issues)

---

## $xtide.events()

`$xtide.events()` returns the upcoming tidal events from the database — as
many days as the `days` option keeps (see
[Configuration](configuration.md)).  Pass a count to cap the list:
`$xtide.events(max_events=4)`.

```cheetah
#for event in $xtide.events()
    $event.location
    $event.dateTime
    $event.eventType
    $event.level
#end for
```

Sample values:

```
$event.location : Palo Alto Yacht Harbor, San Francisco Bay, California
$event.dateTime : 2024-07-11 04:03:00 PDT
$event.eventType: High Tide
$event.level    : 6.34 feet
```

`dateTime` and `level` are WeeWX ValueHelpers: `$event.dateTime` formats
with your report's time settings, and `$event.level` renders in feet or
meters per the report's unit system (`%0.2f`, with a ` feet`/` meters`
label, translated when the report's lang file carries those two words).
`eventType` is the string `High Tide` or `Low Tide`.

## $xtide.graph()

`$xtide.graph()` builds everything the sample report's interactive tide
page shows, for embedding in your own skin.  It returns `None` if the tide
program could not be run (guard for that and render a hint); otherwise:

| Property | What it is |
|---|---|
| `$g.location` | The station name, as the harmonics file spells it |
| `$g.unit` | `ft` or `m`: the report's altitude unit (`group_altitude`), or the harmonics file's own units if the report uses neither |
| `$g.svg_day`, `$g.svg_week`, `$g.svg_month` | Finished inline SVGs: the 2-day, 7-day and 30-day views, in the wide frame |
| `$g.svg_narrow_day`, `$g.svg_narrow_week`, `$g.svg_narrow_month` | The same three views in the narrow frame, for a phone (3.3) |
| `$g.json` | The data payload for `xtide.js` (tabs, tooltip, "now" line, event-list filtering) and `xtide_now.js` (the Right now level and direction) |
| `$g.events` | Display rows for the tide table: `ts`, `eventType` (translated), `high` (true for a high tide), `level_str`, `time_str` |
| `$g.credit` | Where the station's harmonic data come from, as the harmonics file credits it (its Credit, else its Source), ready for markup; empty if the file names neither |

### Two drawings: the wide frame and the phone one

An SVG's text is in viewBox units, so a drawing 1000 units wide shown 334
px across on a phone renders its 12-unit labels at 4 px.  Enlarging the
type in a stylesheet cannot fix that: the gutter and the foot the words sit
in do not grow with them, so bigger labels collide and clip at the frame.

So since 3.3 every view is drawn twice, and one call returns both.  The
wide drawing is what it always was, but for one fix noted below.  The narrow one is
360 units across, so a unit is about a pixel on a phone: its labels are 16
units and read 11.6 px on a 320 px screen.  It spends that room by thinning
what will not fit — a time label every 12 hours on the 2-day view, every
second day on the 7-day, every tenth on the 30-day, a coarser step on the
level axis, a curve with only the vertices 300 units can resolve — and it
drops the 2-day view's inline event labels, which the tooltip covers.

Emit both and let a media query choose; the narrow SVG carries the class
`xg-narrow`, which is also how `xtide.js` tells them apart:

    <div class="xg-wrap" id="xg-wrap-day">
      $g.svg_day
      $g.svg_narrow_day
      <div class="xg-tooltip" id="xg-tip-day"></div>
    </div>

    svg.xg-narrow{display:none}
    svg.xg-narrow .xg-lab{font-size:16px}
    @media (max-width: 600px){
      svg.xg{display:none}
      svg.xg-narrow{display:block}
    }

One fix in 3.3 touches the wide drawing too: `%a`/`%b` come from the locale
weewxd runs under, not from the skin's language file, so a French station
draws `mars 30` where an English one draws `Sep 30`.  The wider word ran past
the right edge, so the last time label of each view now sits two units
further in.  Nothing else about that drawing changed.

Two rules matter if you do this.  Set `.xg-lab` on the narrow drawing to
**16 units** — its gutter, its foot and the spacing of its labels are laid
out for exactly that size, and another size undoes the layout.  And keep
both drawings inside the same `#xg-wrap-<view>`: `xtide.js` looks there for
the one that is visible, measures a pointer against *that* drawing's frame,
and moves the "now" line on both, so a rotation that switches drawings
needs no reload.  Emitting only `$g.svg_day` and its two siblings is still
a complete page, and behaves as it did before 3.3.

### The page's typography: `clock` and `unit_label`

`$xtide.graph()` takes two optional arguments, for a skin embedding the
graph in a page with typography of its own.  Both default to what the
sample report has always done, so `$xtide.graph()` with no arguments is
unchanged.

| Argument | What it does |
|---|---|
| `clock` | `12` or `24`: the page's clock.  Omitted, the clock follows the locale WeeWX runs under |
| `unit_label` | The unit written after each level in `level_str`: `ft` in place of the spelled-out `feet` |

`clock` settles every time the graph writes, in one place: each event row's
`time_str`, the labels on the 2-day view, that view's hour axis, and the
tooltip `xtide.js` draws in the browser.  The tooltip is the one worth
knowing about, because it is the easiest to miss: it formats through the
browser's `Intl`, so it follows each *visitor's* locale, and a reader whose
browser is 24-hour can be shown a 24-hour tooltip over a 12-hour table.
Stating `clock` settles all four together.

`unit_label` replaces the word and nothing else.  `$g.unit` and the
payload's unit stay `ft` or `m`, because skins print those directly.  WeeWX
unit labels carry a leading space by convention (`$unit.label.altitude` is
`' ft'`) and the label is joined with its own single space, so `' ft'` and
`'ft'` both render `7.72 ft`.

Taking the label from the report's own formatter keeps a page's units in
one place:

    #set $g = $xtide.graph(clock=12, unit_label=$unit.label.altitude)

A `clock` that is neither 12 nor 24 is logged and ignored, falling back to
the locale: a typo in `skin.conf` costs a log line, never the page.

The sample skin's `index.html.tmpl` is the reference consumer: it embeds
both drawings of each view, emits `<script>var XTIDE_DATA = $g.json;</script>`, and
loads `xtide.js` (the tabs, the tooltip and the "now" line) and
`xtide_now.js` (the Right now card, the countdowns and the dimmed past
rows).  Everything that depends on the time of day is kept current by
those scripts, so the page never goes stale between reports.  Each script
lists the element ids and classes it looks for at the top of the file;
a skin can use either one without the other.

Before 3.1 each `$g.events` row also carried `icon`, the file name of a
tide icon.  The icons no longer ship, and the key is gone with them: use
`high` to tell the two kinds apart.

`$g.json` is read by the scripts that ship with the skin, and its shape is
theirs to change.  3.3 rearranged it for the second drawing: the single
`layout` became `layouts`, one per frame, and each view's `vlo`/`vhi`
became `scales`, one pair per frame, because a narrow frame spends fewer
gridlines and so rounds to a different value range.  A page that ships the
`xtide.js` of its own version reads its own payload and needs no attention;
only hand-written javascript against the old keys does.

Everything visual is styled through `xg-*` CSS classes
(`xtide.css` in the sample skin) — restyle those in your own stylesheet
rather than editing the SVG generation.  The graph always covers 30 days
and runs the tide program at report time; it does not read the events
database.

[PaloAltoWeather.com's tides page](https://www.paloaltoweather.com/tides.html)
is an example of `$xtide.graph()` embedded in another skin, restyled
entirely through the `xg-*` classes.
