---
title: Home
layout: default
nav_order: 1
permalink: /
description: A WeeWX extension for XTide — an interactive tide graph in nine languages, plus $xtide.events() tags for your own reports.
---

# weewx-xtide

**Tide predictions for WeeWX** — an interactive tide graph, plus
`$xtide.events()` tags for your own reports, powered by
[XTide](https://flaterco.com/xtide/).

[View on GitHub](https://github.com/chaunceygardiner/weewx-xtide){: .btn .btn-primary }
[Download weewx-xtide.zip](https://github.com/chaunceygardiner/weewx-xtide/releases/latest/download/weewx-xtide.zip){: .btn }
[Report an issue](https://github.com/chaunceygardiner/weewx-xtide/issues){: .btn }

weewx-xtide runs XTide's `tide` program for a station you choose, keeps the
high/low tide predictions in its own small database, and serves them to
WeeWX reports two ways:

**The bundled sample report** is a tide page in three parts.  *Right now*
shows the current level, whether the tide is rising or falling, and the
next high or low with a countdown.  The *tide graph* draws continuous tide
levels with the high and low tides marked, tabs for 2-day, 7-day and 30-day
views, night (sunset to sunrise) shading, a "now" line, and click/hover
anywhere on the curve for the exact time and tide level.  The *tide table*
lists the tides for the selected view, with how long until each.  The page
follows the reader's light or dark system setting, keeps its clock-driven
parts current without reloading, and is self-contained — no javascript
libraries, nothing fetched at run time.

![The tide page](https://raw.githubusercontent.com/chaunceygardiner/weewx-xtide/main/XTideSampleReport.png)

![The tide page in dark mode](https://raw.githubusercontent.com/chaunceygardiner/weewx-xtide/main/XTideSampleReport-dark.png)

On a phone the graph card carries a second drawing, laid out for that width
rather than the wide one shrunk — a chart's text scales with the chart, so the
wide drawing's labels came out at about 4 px there:

![The tide page on a phone](https://raw.githubusercontent.com/chaunceygardiner/weewx-xtide/main/XTideSampleReport-phone.png)

The sample report speaks nine languages — English, Danish, Dutch, French,
German, Italian, Norwegian, Spanish and Swedish.  One `lang` line in
weewx.conf switches: see [Translating (i18n)](i18n.md).

**The `$xtide.events()` tag** gives any report the upcoming high and low
tides as data — time, type and level, with WeeWX's own unit and time
formatting.  **`$xtide.graph()`** hands a skin everything the sample page
is built from, for embedding the graph in its own design —
[PaloAltoWeather.com's tides page](https://www.paloaltoweather.com/tides.html)
is built on it.  See [Tags in your skin](tags.md).

## Requirements

- Python 3.10
- WeeWX 4 or 5
- The [xtide](https://flaterco.com/xtide/) package, built from source per
  [Installation](installation.md)

## License

weewx-xtide is licensed under the GNU Public License v3.
Copyright 2024-2026 by John A Kline (john@johnkline.com).
XTide is Copyright 1998 David Flater.  Harmonics data are credited on the
tide page as the installed harmonics file credits them.  Tide predictions
are NOT FOR NAVIGATION.
