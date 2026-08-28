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

The installer seeds an `[XTide]` section in weewx.conf:

```ini
[XTide]
    location = Palo Alto Yacht Harbor, San Francisco Bay, California
    prog = /usr/bin/tide
    days = 7
```

| Option | What it does |
|---|---|
| `location` | The tide station, matched by XTide's own prefix rules.  Stations are listed at [flaterco.com's locations page](https://flaterco.com/xtide/locations.html).  Any station works, in any timezone — all times are handled in UTC internally and displayed in the server's local time. |
| `prog` | Where the `tide` program is.  The default is `/usr/bin/tide` for legacy reasons; an XTide built per [Installation](installation.md) lands at `/usr/local/bin/tide`, so set this. |
| `days` | How many days of tidal events to keep in the database for `$xtide.events()` (default 7).  The sample report's graph is not affected: it always shows 30 days. |
| `data_binding` | The WeeWX data binding (default `xtide_binding`, seeded by the installer along with its database, `xtide.sdb`). |

Tide predictions are deterministic, so the extension fetches once at
startup and then once per local midnight; events are written to the
database at the end of an archive period, and only when they changed.

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
