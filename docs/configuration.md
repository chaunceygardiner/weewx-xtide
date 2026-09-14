---
title: Configuration
layout: default
nav_order: 3
description: The [XTide] options (location, prog, days), enabling $xtide.events() in your reports, the sample report's options, and troubleshooting from the command line.
---

# Configuring weewx-xtide

[weewx-xtide manual](https://chaunceygardiner.github.io/weewx-xtide/) ·
[weewx-xtide on GitHub](https://github.com/chaunceygardiner/weewx-xtide) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-xtide/issues)

---

The installer seeds an `[XTide]` section in weewx.conf.  A fresh install
gets this, each option introduced by a comment (trimmed here):

```ini
[XTide]
    data_binding = xtide_binding
    #days = 7
    location = "Palo Alto Yacht Harbor, San Francisco Bay, California"
    prog = /usr/bin/tide
```

An option written commented out is one the extension supplies for itself.
Leave it commented and the extension's own value governs, including a
better one a later release might bring; uncomment it to pin this station
to the value written there.

Installing an extension fills in options that are missing from weewx.conf
and never rewrites one that is already there, so your own file may not
look like the above: where it reads `#days = 7`, uncomment the line and
change the value; where it reads `days = 7`, just change the value.  The
same goes for an upgrade — it leaves the section you already have alone.

| Option | What it does |
|---|---|
| `location` | The tide station, matched by XTide's own prefix rules.  Stations are listed at [flaterco.com's locations page](https://flaterco.com/xtide/locations.html), or, for whatever harmonics file is installed (see [Stations outside the US](installation.md#stations-outside-the-us)), by `tide -m l`.  Any station works, in any timezone — all times are handled in UTC internally and displayed in the server's local time.  Keep the quotation marks: a name containing commas is read as a list without them. |
| `prog` | Where the `tide` program is.  The default is `/usr/bin/tide` for legacy reasons; an XTide built per [Installation](installation.md) lands at `/usr/local/bin/tide`, so set this. |
| `days` | How many days of tidal events to keep in the database for `$xtide.events()` (default 7).  The sample report's graph is not affected: it always shows 30 days. |
| `data_binding` | The WeeWX data binding (default `xtide_binding`, seeded by the installer along with its database, `xtide.sdb`). |

Tide predictions are deterministic, so the extension fetches once at
startup and then once per local midnight; events are written to the
database at the end of an archive period, and only when they changed.

## Units

There is no units option.  The database stores levels in the harmonics
file's own units, feet or meters, and every report shows them in its own
altitude unit: `unit_system`, or `group_altitude = foot` or `meter` under
the report's `[[[Units]]]`, exactly as for the rest of WeeWX.  That holds
for `$xtide.events()` and for the sample report's graph alike.  A units
preference saved in XTide's own `~/.xtide.xml` is overridden and has no
effect on either.

## The sample report

The installer registers `XTideReport`, which renders the tide page to
`<weewx-html-directory>/xtide`.  It is a normal WeeWX report: set
`HTML_ROOT` to move it, `enable = false` to turn it off, or `lang = de`
(etc.) to translate it — see [Translating (i18n)](i18n.md).  The report
runs the tide program itself at report time; it does not read the events
database.

## $xtide.events() in your own reports

Add `user.xtide.XTideVariables` to each report that should see tidal
events.  For example, for the Seasons report (you may need to add both
lines if they do not already exist):

```ini
[StdReport]
    [[SeasonsReport]]
        [[[CheetahGenerator]]]
            search_list_extensions = user.xtide.XTideVariables
```

Restart WeeWX, then use the tags — see [Tags in your skin](tags.md).

## Troubleshooting

1. Can you run the tide program **as the WeeWX user**?  If not, go no
   further until you resolve that.

1. Did you forget to add `XTideVariables` to your report's
   `search_list_extensions`?

1. The extension can be run from the command line to test.  Activate the
   WeeWX virtual environment first, and point `PYTHONPATH` at WeeWX's
   `USER_ROOT` parent (`/home/weewx/bin` in these examples) so
   `user.xtide` resolves.

   To test running the tide program the way the extension does:
   ```sh
   PYTHONPATH=/home/weewx/bin python3 /home/weewx/bin/user/xtide.py \
       --test-tide-execution \
       --location "Palo Alto Yacht Harbor, San Francisco Bay, California" \
       --prog /usr/local/bin/tide
   ```

   To test the service as a whole — a full engine run, saving to a
   temporary sqlite database and reading it back:
   ```sh
   PYTHONPATH=/home/weewx/bin python3 /home/weewx/bin/user/xtide.py \
       --test-service \
       --location "Palo Alto Yacht Harbor, San Francisco Bay, California" \
       --prog /usr/local/bin/tide
   ```

   To view the tide records in the production database (sqlite only):
   ```sh
   PYTHONPATH=/home/weewx/bin python3 /home/weewx/bin/user/xtide.py \
       --view-events --xtide-database /home/weewx/archive/xtide.sdb
   ```

   To see all options:
   ```sh
   PYTHONPATH=/home/weewx/bin python3 /home/weewx/bin/user/xtide.py --help
   ```

1. On a schema mismatch after an upgrade, the extension logs the mismatch
   and serves nothing.  There is no migration: stop WeeWX, delete
   `xtide.sdb`, and restart — the database holds only forecasts and
   rebuilds itself immediately.
