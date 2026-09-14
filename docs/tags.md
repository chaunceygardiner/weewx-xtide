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
