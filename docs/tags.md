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
| `$g.svg_day`, `$g.svg_week`, `$g.svg_month` | Finished inline SVGs: the 2-day, 7-day and 30-day views |
| `$g.json` | The data payload for `xtide.js` (tabs, tooltip, "now" line, event-list filtering) and `xtide_now.js` (the Right now level and direction) |
| `$g.events` | Display rows for the tide table: `ts`, `eventType` (translated), `high` (true for a high tide), `level_str`, `time_str` |
| `$g.credit` | Where the station's harmonic data come from, as the harmonics file credits it (its Credit, else its Source), ready for markup; empty if the file names neither |

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
the three SVGs, emits `<script>var XTIDE_DATA = $g.json;</script>`, and
loads `xtide.js` (the tabs, the tooltip and the "now" line) and
`xtide_now.js` (the Right now card, the countdowns and the dimmed past
rows).  Everything that depends on the time of day is kept current by
those scripts, so the page never goes stale between reports.  Each script
lists the element ids and classes it looks for at the top of the file;
a skin can use either one without the other.

Before 3.1 each `$g.events` row also carried `icon`, the file name of a
tide icon.  The icons no longer ship, and the key is gone with them: use
`high` to tell the two kinds apart.

Everything visual is styled through `xg-*` CSS classes
(`xtide.css` in the sample skin) — restyle those in your own stylesheet
rather than editing the SVG generation.  The graph always covers 30 days
and runs the tide program at report time; it does not read the events
database.

[PaloAltoWeather.com's tides page](https://www.paloaltoweather.com/tides.html)
is an example of `$xtide.graph()` embedded in another skin, restyled
entirely through the `xg-*` classes.
