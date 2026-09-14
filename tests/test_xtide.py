# Copyright 2026 by John A Kline <john@johnkline.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
"""Tests for weewx-xtide.

Run from the repo root with the WeeWX venv python:
    /home/weewx/weewx-venv/bin/python -m pytest tests

No real tide program is needed: every test drives the code through fake
`tide` executables written to the pytest tmp_path.  TZ is pinned to
America/Los_Angeles for reproducible local-midnight math.
"""
import datetime
import importlib
import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

from unittest import mock

import configobj
import pytest

os.environ['TZ'] = 'America/Los_Angeles'
time.tzset()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'bin', 'user'))

import weewx  # noqa: E402
import weewx.manager  # noqa: E402
import weewx.units  # noqa: E402
import weeutil.config  # noqa: E402
import xtide  # noqa: E402

LOC = 'Palo Alto Yacht Harbor, San Francisco Bay, California'

# Real tide output, both vintages (from the parser comments).  tide is
# always run with -z, so times are UTC (8:12 AM UTC == 1:12 AM PDT).
V216_OUTPUT = '''"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,8:12 AM UTC,8.50 ft,"High Tide"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,12:54 PM UTC,,"Sunrise"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,2:24 PM UTC,,"Moonrise"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,4:31 PM UTC,-0.64 ft,"Low Tide"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,10:41 PM UTC,6.57 ft,"High Tide"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-08,3:32 AM UTC,,"Sunset"
'''
V215_OUTPUT = '''Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,8:12 AM UTC,8.50 ft,High Tide
Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,12:54 PM UTC,,Sunrise
Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,4:31 PM UTC,-0.64 ft,Low Tide
'''

STATION_NOT_FOUND_STDERR = '''-----------------------------------------------------------------------------
            XTide 2   Copyright (C) 1998 David Flater.
This obnoxious message will go away permanently if you create a file in your
home directory called ".disableXTidedisclaimer".
-----------------------------------------------------------------------------
Indexing /usr/local/share/xtide/harmonics.tcd...
XTide Error:  STATION_NOT_FOUND
The specified station was not found in any harmonics file.

Error details:
Could not find: Atlantis, Lost City
'''

# A fake tide that computes a sinusoidal curve for raw mode and synthesized
# events for plain mode, honoring -b/-e/-s/-m/-u.  The extension runs tide with
# -z and bare UTC 'YYYY-MM-DD HH:MM' window arguments (tide_utc_arg), so the
# simulator requires -z, parses the window as UTC, and stamps events in UTC.
#
# Its harmonics are in feet, but like the real tide under a ~/.xtide.xml
# units preference it answers in METERS unless -u says otherwise -- so every
# graph test fails if either the raw or the plain run loses its -u.
SIMULATOR = '''import datetime, math, sys
args = sys.argv[1:]
if '-m' in args and args[args.index('-m') + 1] == 'a':
    # About mode, for the page's credit line; the ampersand proves escaping.
    print('Name                Simulated Harbor')
    print('Credit              Simulated harmonics & tests')
    print('                    https://example.invalid/')
    sys.exit(0)
assert '-z' in args, 'tide must be run with -z (UTC): %%r' %% args
def get(flag):
    return args[args.index(flag) + 1]
units = get('-u') if '-u' in args else 'm'
assert units in ('x', 'ft', 'm'), 'bad -u %%r' %% units
units = 'ft' if units == 'x' else units
scale = 1.0 if units == 'ft' else 0.3048
def when(flag):
    return int(datetime.datetime.strptime(get(flag), '%%Y-%%m-%%d %%H:%%M')
               .replace(tzinfo=datetime.timezone.utc).timestamp())
begin = when('-b')
end = when('-e')
hh, mm = get('-s').split(':')
step = int(hh) * 3600 + int(mm) * 60
LOC = %r
def level(t):
    return scale * (4.0 + 4.0 * math.sin(2 * math.pi * t / (12.42 * 3600)))
def stamp(t):
    dt = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return '%%s,%%s' %% (dt.strftime('%%Y-%%m-%%d'), dt.strftime('%%I:%%M %%p %%Z'))
if get('-m') == 'r':
    print('Location,time_t,Value/unit')
    t = begin
    while t < end:
        print('"%%s",%%d,%%f' %% (LOC, t, level(t)))
        t += step
else:
    t = begin + 3 * 3600
    high = True
    while t < end:
        kind = 'High Tide' if high else 'Low Tide'
        print('"%%s",%%s,%%.2f %%s,"%%s"' %% (LOC, stamp(t), level(t), units, kind))
        high = not high
        t += 22356  # ~6h13m between extremes
    day = begin
    while day < end:
        print('"%%s",%%s,,"Sunrise"' %% (LOC, stamp(day + 6 * 3600)))
        print('"%%s",%%s,,"Sunset"' %% (LOC, stamp(day + 20 * 3600)))
        day += 86400
''' % LOC


@pytest.fixture
def make_tide(tmp_path):
    """Writes an executable fake tide; returns its path."""
    def write(body: str) -> str:
        prog = tmp_path / 'tide'
        prog.write_text('#!%s\n%s' % (sys.executable, body))
        prog.chmod(0o755)
        return str(prog)
    return write


def canned(stdout: str = '', stderr: str = '', rc: int = 0) -> str:
    return ('import sys\n'
            'sys.stdout.write(%r)\n'
            'sys.stderr.write(%r)\n'
            'sys.exit(%d)\n' % (stdout, stderr, rc))


def make_cfg(prog: str, days: int = 7) -> xtide.Configuration:
    return xtide.Configuration(lock=threading.Lock(), location=LOC, prog=prog,
                               days=days, events=[])


class TestEventParser:
    def test_xtide_216_format(self, make_tide):
        cfg = make_cfg(make_tide(canned(V216_OUTPUT)))
        assert xtide.XTidePoller.populate_tidal_events(cfg)
        assert len(cfg.events) == 3  # sun/moon events dropped
        ev = cfg.events[0]
        assert ev.location == LOC
        assert ev.eventType == xtide.EventType.HIGH_TIDE
        assert ev.level == 8.50
        assert ev.usUnits == weewx.US
        dt = datetime.datetime.fromtimestamp(ev.dateTime)
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2024, 7, 7, 1, 12)
        assert cfg.events[1].eventType == xtide.EventType.LOW_TIDE
        assert cfg.events[1].level == -0.64

    def test_xtide_215_format_restores_commas(self, make_tide):
        cfg = make_cfg(make_tide(canned(V215_OUTPUT)))
        assert xtide.XTidePoller.populate_tidal_events(cfg)
        assert len(cfg.events) == 2
        assert cfg.events[0].location == LOC

    def test_metric_units(self, make_tide):
        out = '"%s",2024-07-07,8:12 AM UTC,2.59 m,"High Tide"\n' % LOC
        cfg = make_cfg(make_tide(canned(out)))
        assert xtide.XTidePoller.populate_tidal_events(cfg)
        assert cfg.events[0].usUnits == weewx.METRIC
        assert cfg.events[0].level == 2.59

    def test_fetch_asks_for_the_harmonics_units(self, make_tide, tmp_path):
        # -u x on the database fetch, so a ~/.xtide.xml units preference
        # cannot put the database in anything but the harmonics' own units.
        argv = tmp_path / 'argv.json'
        prog = make_tide('import json, sys\n'
                         'json.dump(sys.argv[1:], open(%r, "w"))\n'
                         'sys.stdout.write(%r)\n' % (str(argv), V216_OUTPUT))
        assert xtide.XTidePoller.populate_tidal_events(make_cfg(prog))
        args = json.loads(argv.read_text())
        assert args[args.index('-u') + 1] == 'x'


class TestUnits:
    """Levels are stored in the harmonics file's units and shown in the
    report's.  $xtide.events() once passed no converter to its
    ValueHelpers, which then converted nothing, and no formatter for
    dateTime, which then ignored the report's time formats."""

    @staticmethod
    def converter(altitude):
        groups = dict(weewx.units.USUnits)
        if altitude is not None:
            groups['group_altitude'] = altitude
        return weewx.units.Converter(groups)

    @staticmethod
    def variables(converter, texts=None, formatter=None, rows=None, prog=None):
        """A real XTideVariables over a stub generator; the database read
        is replaced by rows."""
        generator = mock.Mock()
        generator.converter = converter
        generator.formatter = formatter or weewx.units.Formatter()
        generator.skin_dict = {'Texts': texts or {}}
        generator.config_dict = {'XTide': {'location': LOC, 'prog': prog or 'tide'}}
        variables = xtide.XTideVariables(generator)
        variables.getEventRows = lambda max_events=None: [dict(r) for r in rows or []]
        return variables

    ROWS = [
        {'dateTime': 1789371000, 'usUnits': weewx.METRIC, 'location': LOC,
         'eventType': xtide.EventType.HIGH_TIDE, 'level': 2.01},
        {'dateTime': 1789393000, 'usUnits': weewx.US, 'location': LOC,
         'eventType': xtide.EventType.LOW_TIDE, 'level': 6.59},
    ]

    def test_tide_units_follow_the_report(self):
        assert xtide.tide_units(self.converter('foot')) == 'ft'
        assert xtide.tide_units(self.converter('meter')) == 'm'
        assert xtide.tide_units(weewx.units.Converter(weewx.units.MetricUnits)) == 'm'
        assert xtide.tide_units(weewx.units.Converter(weewx.units.MetricWXUnits)) == 'm'
        # An altitude unit tide cannot draw in: the harmonics' own units.
        assert xtide.tide_units(self.converter('mile')) == 'x'

    def test_events_convert_to_the_report_units(self):
        feet = self.variables(self.converter('foot'), rows=self.ROWS).events()
        assert str(feet[0]['level']) == '6.59 feet'     # stored in meters
        assert str(feet[1]['level']) == '6.59 feet'
        meters = self.variables(self.converter('meter'), rows=self.ROWS).events()
        assert str(meters[0]['level']) == '2.01 meters'
        assert str(meters[1]['level']) == '2.01 meters'  # stored in feet

    def test_events_use_the_report_time_format(self):
        formatter = weewx.units.Formatter(time_format_dict={'current': 'at %H:%M'})
        events = self.variables(self.converter('foot'), formatter=formatter,
                                rows=self.ROWS).events()
        expected = datetime.datetime.fromtimestamp(self.ROWS[0]['dateTime']).strftime('at %H:%M')
        assert str(events[0]['dateTime']) == expected

    def test_events_unit_label_is_translated(self):
        de = lang_texts('de')
        events = self.variables(self.converter('foot'), texts=de, rows=self.ROWS).events()
        assert str(events[0]['level']) == '6.59 Fuß'
        events = self.variables(self.converter('meter'), texts=de, rows=self.ROWS).events()
        assert str(events[0]['level']) == '2.01 Meter'

    @pytest.mark.parametrize('units, unit, long_form, peak', [
        ('ft', 'ft', ' feet', 8.0), ('m', 'm', ' meters', 8.0 * 0.3048), ('x', 'ft', ' feet', 8.0)])
    def test_graph_draws_in_the_units_asked_for(self, make_tide, units, unit, long_form, peak):
        # SIMULATOR answers in meters unless -u says otherwise, so the curve
        # (raw mode) and the events (plain mode) both prove -u reached them.
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC, units=units).build()
        assert g is not None
        assert g.unit == unit
        payload = json.loads(g.json)
        assert payload['unit'] == unit
        assert max(payload['views']['day']['samples']) == pytest.approx(peak, abs=0.05)
        # The events' label proves -u reached plain mode: without it they
        # come back in meters.
        assert all(ev['level_str'].endswith(long_form) for ev in g.events)

    def test_graph_tag_passes_the_report_units(self, make_tide):
        prog = make_tide(SIMULATOR)
        g = self.variables(self.converter('meter'), prog=prog).graph()
        assert g is not None and g.unit == 'm'
        g = self.variables(self.converter('foot'), prog=prog).graph()
        assert g is not None and g.unit == 'ft'


class TestTideFailures:
    def test_nonzero_exit_logs_tide_error(self, make_tide, caplog):
        prog = make_tide(canned(stderr='XTide Fatal Error:  BAD_OR_AMBIGUOUS_COMMAND_LINE\n', rc=255))
        assert xtide.XTidePoller.populate_tidal_events(make_cfg(prog)) is False
        assert 'BAD_OR_AMBIGUOUS_COMMAND_LINE' in caplog.text

    def test_unknown_station_exits_zero_but_logs(self, make_tide, caplog):
        # tide reports an unknown station with rc=0 and the error on stderr
        prog = make_tide(canned(stderr=STATION_NOT_FOUND_STDERR, rc=0))
        assert xtide.XTidePoller.populate_tidal_events(make_cfg(prog)) is False
        assert 'STATION_NOT_FOUND' in caplog.text

    def test_missing_prog(self, caplog):
        assert xtide.XTidePoller.populate_tidal_events(make_cfg('/nonexistent/tide')) is False
        assert 'not found' in caplog.text

    def test_timeout(self, make_tide, caplog, monkeypatch):
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd='tide', timeout=10)
        monkeypatch.setattr(xtide.subprocess, 'run', raise_timeout)
        assert xtide.XTidePoller.populate_tidal_events(make_cfg('/usr/bin/true')) is False
        assert 'timed out' in caplog.text

    def test_extract_tide_error_skips_disclaimer(self):
        msg = xtide.XTidePoller.extract_tide_error(STATION_NOT_FOUND_STDERR)
        assert msg.startswith('XTide Error:  STATION_NOT_FOUND')
        assert 'obnoxious' not in msg
        assert xtide.XTidePoller.extract_tide_error('plain complaint') == 'plain complaint'


class TestGraphBuilder:
    @pytest.fixture
    def graph(self, make_tide):
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        assert g is not None
        return g

    def test_views_windows_and_sampling(self, graph):
        payload = json.loads(graph.json)
        assert graph.unit == 'ft'
        for name, days, step in xtide.XTideGraphBuilder.VIEWS:
            view = payload['views'][name]
            assert view['t1'] - view['t0'] == days * 86400
            assert view['step'] == step
            assert len(view['samples']) == days * 86400 // step
            # t0 is a local midnight
            dt = datetime.datetime.fromtimestamp(view['t0'])
            assert (dt.hour, dt.minute) == (0, 0)
            assert view['vlo'] < min(view['samples'])
            assert view['vhi'] > max(view['samples'])

    def test_payload_events_and_display_list_agree(self, graph):
        assert graph.events
        payload = json.loads(graph.json)
        assert len(payload['events']) == len(graph.events)
        first = graph.events[0]
        assert first['eventType'] in ('High Tide', 'Low Tide')
        assert first['high'] is (first['eventType'] == 'High Tide')
        assert 'icon' not in first     # the icons were retired in 3.1
        assert first['level_str'].endswith(' feet')

    def test_svgs_have_expected_parts(self, graph):
        for svg in (graph.svg_day, graph.svg_week, graph.svg_month):
            for cls in ('xg-curve', 'xg-night', 'xg-nowline', 'xg-frame', 'xg-hi', 'xg-lo'):
                assert cls in svg, 'missing %s' % cls
        assert 'xg-evlab' in graph.svg_day        # labels on the day view only
        assert 'xg-evlab' not in graph.svg_month
        # The halo paints its stroke under the glyphs only with this
        # attribute; xtide.css cannot say it (the Nu CSS checker rejects it).
        labels = re.findall(r'<text class="xg-lab xg-evlab"[^>]*>', graph.svg_day)
        assert labels and all('paint-order="stroke"' in t for t in labels)
        assert 'paint-order' not in re.sub(r'/\*.*?\*/', '', open(CSS_PATH).read(), flags=re.S)

    def test_build_fails_gracefully(self, make_tide):
        prog = make_tide(canned(stderr=STATION_NOT_FOUND_STDERR, rc=0))
        assert xtide.XTideGraphBuilder(prog, LOC).build() is None

    def test_credit_comes_from_about_mode_escaped(self, graph):
        # The Credit line, not its continuation line, and markup-escaped.
        assert graph.credit == 'Simulated harmonics &amp; tests'

    def test_credit_falls_back_to_source(self, make_tide):
        # openwatersio's files carry Source and no Credit.
        about = ('Name                Brest, Brittany, France\n'
                 'Source              TICON-4\n'
                 'Restriction         Public Domain\n')
        builder = xtide.XTideGraphBuilder(make_tide(canned(about)), LOC)
        assert builder.get_credit() == 'TICON-4'

    def test_a_failed_credit_costs_only_the_credit(self, make_tide, tmp_path):
        builder = xtide.XTideGraphBuilder(make_tide(canned(stderr='XTide Fatal Error: X\n', rc=1)), LOC)
        assert builder.get_credit() == ''
        assert xtide.XTideGraphBuilder('/nonexistent/tide', LOC).get_credit() == ''
        # A program that exists but cannot be run raises PermissionError, an
        # OSError that is not FileNotFoundError; it must not escape either.
        unrunnable = tmp_path / 'tide-not-executable'
        unrunnable.write_text('#!/bin/sh\n')
        unrunnable.chmod(0o644)
        assert xtide.XTideGraphBuilder(str(unrunnable), LOC).get_credit() == ''

    def test_night_intervals(self):
        night = xtide.XTideGraphBuilder.night_intervals
        suns = [(100, 'Sunset'), (200, 'Sunrise'), (300, 'Sunset'), (400, 'Sunrise')]
        assert night(suns, 0, 500) == [(100.0, 200.0), (300.0, 400.0)]
        # clipped to the window
        assert night(suns, 150, 350) == [(150.0, 200.0), (300.0, 350.0)]
        # trailing sunset extends to the window end
        assert night([(100, 'Sunset')], 0, 500) == [(100.0, 500.0)]
        # leading sunrise without a sunset is ignored
        assert night([(100, 'Sunrise')], 0, 500) == []

    def test_choose_tick(self):
        choose = xtide.XTideGraphBuilder.choose_tick
        assert choose(3.0) == 0.5
        assert choose(7.0) == 1.0
        assert choose(12.0) == 2.0
        assert choose(35.0) == 5.0

    def test_event_labels_never_cover_their_markers(self):
        """An extreme near the frame used to have its label clamped inside
        the plot, on top of its own marker (a 7.88 ft high on a 0 to 8
        scale).  Levels of 7.9 and 0.1 put a high 4 px under the top edge
        and a low 4 px over the bottom one.  Each label's text box, taken
        as baseline - 9 to baseline + 2 for 11 px type, must clear its
        marker and stay inside the plot."""
        builder = xtide.XTideGraphBuilder('tide', LOC)
        t0 = int(datetime.datetime(2026, 9, 14).timestamp())
        values = [0.1, 7.9] * 240                          # 2 days at 6 minutes
        tides = [(t0 + 3600, 7.9, 1), (t0 + 7200, 0.1, 2),   # at the frame
                 (t0 + 40000, 4.0, 1), (t0 + 60000, 4.0, 2)]  # mid-plot
        svg, _, _ = builder.build_view_svg('day', t0, t0 + 2 * 86400, 360, values, tides, [], 'ft')
        pairs = re.findall(r'<circle class="xg-(?:hi|lo)" cx="[\d.]+" cy="([\d.]+)" r="([\d.]+)"/>'
                           r'<text class="xg-lab xg-evlab" x="[\d.]+" y="([\d.]+)"[^>]*>', svg)
        assert len(pairs) == 4
        top_edge = builder.MT
        bottom_edge = builder.H - builder.MB
        for cy, r, ly in ((float(a), float(b), float(c)) for a, b, c in pairs):
            text_top, text_bottom = ly - 9, ly + 2
            assert text_bottom < cy - r or text_top > cy + r, \
                'label at y=%.1f covers its marker at y=%.1f' % (ly, cy)
            assert top_edge <= text_top and text_bottom <= bottom_edge


class TestLocaleRobustness:
    """tide's csv is always English, whatever the locale; parsing must not
    depend on strptime's locale-aware %p (most non-English locales define
    empty AM/PM designators, which used to make startup crash), and time
    labels must fall back to 24-hour form under such locales."""

    def test_tide_event_ts(self):
        utc = datetime.timezone.utc
        def epoch(h, m):
            return int(datetime.datetime(2026, 8, 6, h, m, tzinfo=utc).timestamp())
        assert xtide.tide_event_ts('2026-08-06', '9:16 AM UTC') == epoch(9, 16)
        assert xtide.tide_event_ts('2026-08-06', '9:16 PM UTC') == epoch(21, 16)
        assert xtide.tide_event_ts('2026-08-06', '12:00 AM UTC') == epoch(0, 0)
        assert xtide.tide_event_ts('2026-08-06', '12:00 PM UTC') == epoch(12, 0)
        for bad in ('9:16 UTC', '9:16 AM PDT', '13:16 PM UTC', '0:16 AM UTC', 'garbage'):
            with pytest.raises(ValueError):
                xtide.tide_event_ts('2026-08-06', bad)

    def test_parse_and_helper_under_empty_ampm_locale(self, tmp_path):
        # Compile da_DK (empty AM/PM designators) into a private LOCPATH and
        # run the assertions in a subprocess: LC_ALL is read at startup there,
        # sidestepping setlocale-order pitfalls in this process.  localedef
        # exits non-zero on mere warnings, so trust the output dir instead.
        locdir = tmp_path / 'locales'
        locdir.mkdir()
        made = subprocess.run(['localedef', '-i', 'da_DK', '-f', 'UTF-8',
                               str(locdir / 'da_DK.UTF-8')],
                              capture_output=True, encoding='utf-8')
        if not (locdir / 'da_DK.UTF-8').is_dir():
            pytest.skip('cannot compile da_DK locale: %s' % made.stderr.strip())
        expected = int(datetime.datetime(2026, 8, 6, 9, 16,
                                         tzinfo=datetime.timezone.utc).timestamp())
        script = (
            "import locale\n"
            "locale.setlocale(locale.LC_ALL, '')\n"  # what weewxd does
            "assert locale.nl_langinfo(locale.AM_STR) == '', (\n"
            "    'locale did not take: %r' % locale.nl_langinfo(locale.AM_STR))\n"
            "import xtide\n"
            "assert xtide.tide_event_ts('2026-08-06', '9:16 AM UTC') == " + str(expected) + "\n"
            "assert not xtide.use_12_hour_labels()\n")
        env = dict(os.environ, LOCPATH=str(locdir), LC_ALL='da_DK.UTF-8',
                   PYTHONPATH=os.path.join(REPO, 'bin', 'user'))
        run = subprocess.run([sys.executable, '-c', script],
                             capture_output=True, encoding='utf-8', env=env)
        assert run.returncode == 0, run.stderr

    def test_labels_stay_12_hour_in_english_locales(self, make_tide):
        # The test process runs under C/English (AM/PM designators present):
        # every label keeps the traditional 12-hour form, unchanged.
        assert xtide.use_12_hour_labels()
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        assert g is not None
        assert all(ev['time_str'].endswith((' AM', ' PM')) for ev in g.events)
        assert '>6 AM<' in g.svg_day       # axis hour label
        assert re.search(r'\d{1,2}:\d{2} [AP]M</text>', g.svg_day)  # event label

    def test_labels_switch_to_24_hour(self, make_tide, monkeypatch):
        monkeypatch.setattr(xtide, 'use_12_hour_labels', lambda: False)
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        assert g is not None
        for ev in g.events:
            assert re.search(r'\d{2}:\d{2}$', ev['time_str'])
        assert 'AM' not in g.svg_day and 'PM' not in g.svg_day
        assert '>06<' in g.svg_day         # axis hour label, leading zero kept


class TestSampleTemplate:
    """End-to-end Cheetah render.  Compilation alone is NOT sufficient: with
    #errorCatcher Echo, failures render as un-substituted placeholders."""

    def render(self, graph, texts=None, lang='en'):
        from Cheetah.Template import Template
        texts = texts or {}
        class StubXTide:
            def graph(self):
                return graph
        def gettext(key):
            # What core weewx provides: the [Texts] value, English fallback.
            val = texts.get(key, key)
            return val if isinstance(val, str) else key
        tmpl = Template(file=os.path.join(REPO, 'skins', 'xtide', 'index.html.tmpl'),
                        searchList=[{'xtide': StubXTide(), 'gettext': gettext, 'lang': lang}])
        return str(tmpl)

    def test_renders_graph_page(self, make_tide):
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        html = self.render(g)
        assert LOC in html
        assert 'id="xg-wrap-day"' in html
        assert 'id="xt-now"' in html
        assert 'var XTIDE_DATA = {' in html
        assert '<script src="xtide.js"></script>' in html
        assert '<script src="xtide_now.js"></script>' in html
        # Rows before the render are pre-dimmed; either way one row per event.
        assert len(re.findall(r'<div class="xg-evrow(?: past)?" data-ts="\d+">', html)) == len(g.events)
        assert html.count('class="ev-k ev-hi"') + html.count('class="ev-k ev-lo"') == len(g.events)
        assert 'Simulated harmonics &amp; tests' in html
        assert 'xtide_icons' not in html
        # The first paint names the next tide: the first event after now.
        nxt = next(ev for ev in g.events if ev['ts'] > time.time())
        assert '<span class="rel" data-ts="%d">' % nxt['ts'] in html
        assert '$g' not in html  # no un-substituted placeholders (errorCatcher Echo)
        assert '$n' not in html

    def test_renders_failure_page(self):
        html = self.render(None)
        assert 'No tidal data to display' in html
        assert 'xg-wrap-day' not in html
        assert '$g' not in html

    def test_no_hex_colors_in_template(self):
        # Cheetah owns '#': colors belong in xtide.css, never in the template.
        text = open(os.path.join(REPO, 'skins', 'xtide', 'index.html.tmpl')).read()
        assert not re.search(r'#[0-9a-fA-F]{6}', text)


# ---- the stylesheet's two palettes -------------------------------------
#
# Every rule runs over BOTH palettes, and light must pass unchanged: a rule
# only the new dark values satisfy has been fitted to them.  (weewx-nws's
# tests/test_nws_css.py is the pattern.)  GROUNDS pairs each token with the
# grounds it can reach, read from the RULES in xtide.css, and what it is
# there: text or a graphical mark.  A rule that puts a token on a new ground
# must be added here, or this cannot see it.
#
# THE BAR IS THE STRICTER OF TWO MEASURES (John, 2026-09-14: the standard
# weewx-liveseasons uses).  The WCAG 2 ratio is known to overrate some
# pairs -- it passed this stylesheet's first dark palette, whose muted gray
# APCA scores Lc 49 -- so text must clear WCAG 4.5 AND APCA Lc 60, large
# text included, and marks WCAG 3.0 AND Lc 30.

CSS_PATH = os.path.join(REPO, 'skins', 'xtide', 'xtide.css')

BARS = {'text': (4.5, 60), 'mark': (3.0, 30)}

GROUNDS = {
    '--fc-ink':       [('--fc-page', 'text'), ('--fc-surface', 'text'), ('--xg-tip', 'text')],
    '--fc-ink-3':     [('--fc-page', 'text'), ('--fc-surface', 'text'), ('--fc-tint-2', 'text')],
    # Past table rows are muted text on the card, as well as the captions.
    '--fc-muted':     [('--fc-page', 'text'), ('--fc-surface', 'text'), ('--fc-tint-2', 'text')],
    '--fc-accent':    [('--fc-surface', 'text')],
    '--fc-on-accent': [('--fc-accent', 'text')],
    # The Right now level is text, however large; the curve is a 2px mark.
    '--xg-curve':     [('--fc-surface', 'text'), ('--xg-night', 'mark')],
    # Table text and the direction word on the card; markers in the graph,
    # which can sit on the night band.
    '--xg-hi':        [('--fc-surface', 'text'), ('--xg-night', 'mark')],
    '--xg-lo':        [('--fc-surface', 'text'), ('--xg-night', 'mark')],
    '--xg-lab':       [('--fc-surface', 'text')],
    '--xg-evlab':     [('--fc-surface', 'text'), ('--xg-night', 'text')],
    '--xg-unitlab':   [('--fc-surface', 'text'), ('--xg-night', 'text')],
    '--xg-frame':     [('--fc-surface', 'mark')],
    '--xg-now':       [('--fc-surface', 'mark'), ('--xg-night', 'mark')],
}


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _srgb_linear(v):
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def contrast(a, b):
    """The WCAG 2 ratio of two '#rrggbb' colors."""
    def luminance(h):
        r, g, b = (_srgb_linear(v / 255.0) for v in _rgb(h))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def apca(text, ground):
    """APCA Lc (APCA-W3 0.0.98G-4g) of '#rrggbb' text on a '#rrggbb' ground:
    positive for dark text on a lighter ground, negative for light on dark.
    The same constants and steps as weewx-liveseasons' tools/contrast.py, so
    the two skins measure alike; TestPalettes pins them to APCA's published
    fixed points."""
    def y(h):
        v = sum(k * (c / 255.0) ** 2.4 for k, c in zip((0.2126729, 0.7151522, 0.0721750), _rgb(h)))
        return v + (0.022 - v) ** 1.414 if v <= 0.022 else v
    ty, gy = y(text), y(ground)
    if abs(gy - ty) < 0.0005:
        return 0.0
    if gy > ty:
        s = (gy ** 0.56 - ty ** 0.57) * 1.14
        return 0.0 if s < 0.1 else (s - 0.027) * 100
    s = (gy ** 0.65 - ty ** 0.62) * 1.14
    return 0.0 if s > -0.1 else (s + 0.027) * 100


def css_palettes():
    css = open(CSS_PATH).read()
    tokens = r'(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})'
    light = dict(re.findall(tokens, re.search(r':root\s*\{(.*?)\n\}', css, re.S).group(1)))
    dark_block = re.search(r'@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{(.*?)\n\}',
                           css, re.S)
    assert dark_block, 'no prefers-color-scheme dark block in xtide.css'
    dark_only = dict(re.findall(tokens, dark_block.group(1)))
    return light, dict(light, **dark_only), dark_only


class TestPalettes:
    def test_the_measures_are_the_published_ones(self):
        """The oracle.  A passing sweep proves nothing about the arithmetic:
        a wrong exponent or a swapped sign passes pairs as easily as it fails
        them.  APCA-W3 publishes Lc 106.04 for black on white and -107.88
        for white on black; the WCAG ratio of the two is 21 by definition."""
        assert abs(apca('#000000', '#ffffff') - 106.04) < 0.01
        assert abs(apca('#ffffff', '#000000') + 107.88) < 0.01
        assert abs(contrast('#000000', '#ffffff') - 21.0) < 1e-9

    @pytest.mark.parametrize('name', ['light', 'dark'])
    def test_every_token_clears_both_bars_on_every_ground(self, name):
        light, dark, _ = css_palettes()
        palette = light if name == 'light' else dark
        bad = []
        for tok, pairs in sorted(GROUNDS.items()):
            for ground, kind in pairs:
                ratio = contrast(palette[tok], palette[ground])
                lc = apca(palette[tok], palette[ground])
                wcag_bar, apca_bar = BARS[kind]
                if ratio + 1e-9 < wcag_bar or abs(lc) < apca_bar:
                    bad.append('%s on %s (%s): WCAG %.2f, APCA Lc %.1f' % (tok, ground, kind, ratio, lc))
        assert not bad, ('%s palette misses WCAG %s / APCA %s: %s'
                         % (name, BARS['text'][0], BARS['text'][1], '; '.join(bad)))

    @pytest.mark.parametrize('name', ['light', 'dark'])
    def test_the_text_tiers_stay_distinct(self, name):
        """Ink, secondary ink and muted are three levels.  Raising the dark
        muted gray to clear Lc 60 first put it within 2 Lc of --fc-ink-3,
        which erased the difference between them; a tier has to stay a
        tier, not just pass."""
        light, dark, _ = css_palettes()
        palette = light if name == 'light' else dark
        card = palette['--fc-surface']
        ink, ink3, muted = (abs(apca(palette[t], card)) for t in ('--fc-ink', '--fc-ink-3', '--fc-muted'))
        assert ink - ink3 >= 5 and ink3 - muted >= 5, (
            '%s: ink %.1f, ink-3 %.1f, muted %.1f on the card' % (name, ink, ink3, muted))

    def test_nothing_is_dimmed_by_opacity(self):
        """Opacity is a color, and one no token table can see: past tide rows
        at opacity .45 measured Lc 13 to 53.  They are muted by color now,
        and nothing in the stylesheet may reach for opacity again."""
        css = re.sub(r'/\*.*?\*/', '', open(CSS_PATH).read(), flags=re.S)
        assert 'opacity' not in css
        past = re.search(r'\.fc \.xg-evrow\.past,[^{]*\{([^}]*)\}', css)
        assert past and 'var(--fc-muted)' in past.group(1)

    def test_every_light_token_has_a_dark_value(self):
        """A token defined only in :root keeps its LIGHT value on a dark page,
        which is how one unreadable element survives a theme."""
        light, _, dark_only = css_palettes()
        assert sorted(set(light) - set(dark_only)) == []
        assert sorted(set(dark_only) - set(light)) == []

    def test_the_page_stays_behind_the_cards(self):
        """The page is the recessed ground in BOTH themes; a page brighter
        than its cards inverts the figure.  Contrast against black is
        monotonic in luminance, so it orders the two."""
        light, dark, _ = css_palettes()
        for palette in (light, dark):
            assert (contrast(palette['--fc-page'], '#000000')
                    < contrast(palette['--fc-surface'], '#000000'))

    def test_no_color_literal_outside_the_palettes(self):
        """Every color in a rule is a token, so the dark block reaches it."""
        css = open(CSS_PATH).read()
        body = re.sub(r':root\s*\{.*?\n\}', '', css, flags=re.S)
        body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
        assert re.findall(r'#[0-9a-fA-F]{3,6}\b', body) == []


LANG_DIR = os.path.join(REPO, 'skins', 'xtide', 'lang')
LANG_CODES = ('en', 'de', 'fr', 'nl', 'es', 'da', 'it', 'no', 'sv')


def lang_texts(code):
    import configobj
    path = os.path.join(LANG_DIR, '%s.conf' % code)
    return dict(configobj.ConfigObj(path, encoding='utf-8', file_error=True)['Texts'])


class TestI18n:
    """The i18n contract: lang/en.conf ships exactly the $gettext/_t/_raw
    keys that render (both directions), every language carries the same key
    set, placeholders survive translation, and a German report renders
    German.  Keys are single-line literals at every call site so these
    tests can read them from the sources."""

    @staticmethod
    def rendered_keys():
        tmpl = open(os.path.join(REPO, 'skins', 'xtide', 'index.html.tmpl')).read()
        keys = set(m.group(2) for m in re.finditer(r'\$gettext\((["\'])(.+?)\1\)', tmpl))
        src = open(os.path.join(REPO, 'bin', 'user', 'xtide.py')).read()
        keys |= set(m.group(1) for m in re.finditer(r"self\._(?:t|raw)\(\s*'([^']+)'", src))
        return keys

    @staticmethod
    def js_keys():
        keys = set()
        for name in ('xtide.js', 'xtide_now.js'):
            js = open(os.path.join(REPO, 'skins', 'xtide', name)).read()
            for m in re.finditer(r"\btr\(([^)]*)\)", js):
                keys |= set(re.findall(r"'([^']+)'", m.group(1)))
        return keys

    def test_en_conf_ships_exactly_what_renders(self):
        rendered = self.rendered_keys()
        assert rendered, 'no $gettext/_t/_raw keys found: extraction broken?'
        en = lang_texts('en')
        assert set(en) == rendered
        for key, val in en.items():
            assert val == key, 'en.conf must be the identity: %r' % key

    def test_every_language_carries_the_same_key_set(self):
        en_keys = set(lang_texts('en'))
        for code in LANG_CODES[1:]:
            assert set(lang_texts(code)) == en_keys, '%s.conf key set differs' % code

    def test_placeholders_survive_translation(self):
        # Translators may reorder {named} placeholders, never rename them.
        for code in LANG_CODES:
            for key, val in lang_texts(code).items():
                assert (set(re.findall(r'\{(\w+)\}', key))
                        == set(re.findall(r'\{(\w+)\}', val))), \
                    '%s.conf placeholder mismatch for %r' % (code, key)

    def test_translated_date_formats_are_valid_strftime(self):
        when = datetime.datetime(2026, 8, 6, 9, 16)
        for code in LANG_CODES:
            for key, val in lang_texts(code).items():
                if key.startswith('%'):
                    assert when.strftime(val), '%s.conf bad format %r' % (code, val)

    def test_js_keys_come_from_the_payload(self, make_tide):
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        payload_t = json.loads(g.json)['T']
        assert self.js_keys() <= set(payload_t), \
            'xtide.js tr() key missing from the payload T dict'
        for key, val in payload_t.items():
            assert val == key  # no texts passed: English identity

    def test_german_graph_and_page(self, make_tide):
        de = lang_texts('de')
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC, de).build()
        assert g is not None
        for ev in g.events:
            assert ev['eventType'] in ('Hochwasser', 'Niedrigwasser')
            assert ev['level_str'].endswith(' Fuß')
            # de maps both long-form keys to a 24-hour day-first form
            assert re.search(r'\d{2}:\d{2}$', ev['time_str'])
            assert 'AM' not in ev['time_str'] and 'PM' not in ev['time_str']
        assert 'Wasserstand (ft)' in g.svg_day
        payload = json.loads(g.json)
        assert payload['T']['High Tide'] == 'Hochwasser'
        assert payload['T']['{n} tidal events.'] == '{n} Gezeitenereignisse.'
        html = TestSampleTemplate().render(g, texts=de, lang='de')
        assert '<html lang="de">' in html
        assert '<title>Gezeiten</title>' in html
        assert '2 Tage' in html
        assert '$g' not in html
        failure = TestSampleTemplate().render(None, texts=de, lang='de')
        assert 'Keine Gezeitendaten' in failure
        assert '$g' not in failure


REAL_TIDE = os.environ.get('XTIDE_PROG', '/usr/local/bin/tide')


@pytest.fixture(scope='class')
def real_tide_cfg():
    """A Configuration populated by the REAL tide program.

    These integration tests exist because the hermetic tests above only
    validate our assumptions about tide's output format — an xtide upgrade
    that changes the format (as 2.16 did) can only be caught here.

    A missing or non-working tide is a FAILURE, never a skip: anyone working
    on weewx-xtide has tide installed, and a tide that is absent or returns
    no events is the early signal that the extension will not work in
    production (broken installation, or a harmonics upgrade that lost the
    station).
    """
    assert os.access(REAL_TIDE, os.X_OK), (
        'tide program not found at %s -- weewx-xtide cannot work without it '
        '(set XTIDE_PROG if it lives elsewhere)' % REAL_TIDE)
    cfg = make_cfg(REAL_TIDE, days=2)
    assert xtide.XTidePoller.populate_tidal_events(cfg) and cfg.events, (
        'tide is installed at %s but returned no events for %r -- '
        'broken installation or harmonics?  (check the log output above)'
        % (REAL_TIDE, LOC))
    return cfg


class TestRealTide:
    def run_real_tide(self, mode: str, hours: int, step: str = '01:00') -> str:
        begin = datetime.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        out = subprocess.run([REAL_TIDE, '-z', '-l', LOC, '-b', xtide.tide_utc_arg(begin),
                              '-e', xtide.tide_utc_arg(begin + hours * 3600),
                              '-fc', '-m', mode, '-s', step],
                             capture_output=True, encoding='utf-8', timeout=10)
        assert out.returncode == 0, out.stderr
        return out.stdout

    def test_events_are_sane(self, real_tide_cfg):
        events = real_tide_cfg.events
        assert len(events) >= 5  # ~4 extremes per day over 2 days
        for prev, cur in zip(events, events[1:]):
            assert cur.dateTime > prev.dateTime
            # high and low tides strictly alternate
            assert cur.eventType != prev.eventType
        for ev in events:
            assert ev.location == LOC
            assert ev.usUnits in (weewx.US, weewx.METRIC)
            assert -10.0 < ev.level < 20.0

    def test_plain_mode_format_contract(self, real_tide_cfg):
        # The parser assumes 5-column csv with a "<level> <unit>" 4th column
        # on tide rows.  This is what breaks when xtide changes its format.
        import csv as csvmod
        rows = [next(csvmod.reader([line])) for line in self.run_real_tide('p', 48).splitlines()]
        tide_rows = [c for c in rows if len(c) == 5 and c[4] in ('High Tide', 'Low Tide')]
        assert tide_rows, 'no 5-column High/Low Tide rows: format changed?'
        for cols in tide_rows:
            level, unit = cols[3].split(' ')
            float(level)
            assert unit in ('ft', 'm')
            # tide is run with -z, so times must come back labeled UTC
            assert cols[2].endswith(' UTC'), 'expected a UTC time, got %r' % cols[2]
            xtide.tide_event_ts(cols[1], cols[2])

    def test_raw_mode_format_contract(self, real_tide_cfg):
        import csv as csvmod
        lines = self.run_real_tide('r', 6, step='00:30').splitlines()
        data = [next(csvmod.reader([line])) for line in lines if not line.startswith('Location,')]
        assert len(data) >= 10
        times = [int(c[1]) for c in data]
        for cols in data:
            assert len(cols) == 3
            float(cols[2])
        steps = {b - a for a, b in zip(times, times[1:])}
        assert steps == {1800}, 'raw samples not uniformly spaced: %s' % steps

    def test_graph_builder_against_real_tide(self, real_tide_cfg):
        graph = xtide.XTideGraphBuilder(REAL_TIDE, LOC).build()
        assert graph is not None
        # Flater's file names who processed the data; this is the page's credit.
        assert graph.credit.startswith('NOAA data'), graph.credit
        payload = json.loads(graph.json)
        assert len(payload['events']) >= 100  # ~116 extremes in 30 days
        # The plain-mode extremes must sit on the raw-mode curve: at an
        # extreme the curve is flat, so the nearest 6-minute sample is close.
        view = payload['views']['day']
        for ts, level, _ in payload['events']:
            if not view['t0'] <= ts <= view['t1']:
                continue
            idx = min(max(round((ts - view['t0']) / view['step']), 0), len(view['samples']) - 1)
            assert abs(level - view['samples'][idx]) < 0.25, \
                'event at %d (%.2f) is off the curve (%.2f)' % (ts, level, view['samples'][idx])

    def test_a_units_preference_file_is_overridden(self, tmp_path, monkeypatch):
        """A ~/.xtide.xml units preference changes what tide prints when -u
        is absent (measured: Palo Alto in meters).  The premise is checked
        first, so this cannot pass on a tide that ignores the file."""
        (tmp_path / '.xtide.xml').write_text('<?xml version="1.0"?>\n<xtideoptions u="m"/>\n')
        monkeypatch.setenv('HOME', str(tmp_path))
        assert ' m,"High Tide"' in self.run_real_tide('p', 48), \
            'premise: tide no longer honors ~/.xtide.xml units'
        cfg = make_cfg(REAL_TIDE, days=2)
        assert xtide.XTidePoller.populate_tidal_events(cfg) and cfg.events
        assert all(ev.usUnits == weewx.US for ev in cfg.events)   # Palo Alto is in feet
        graph = xtide.XTideGraphBuilder(REAL_TIDE, LOC, units='x').build()
        assert graph is not None and graph.unit == 'ft'
        graph = xtide.XTideGraphBuilder(REAL_TIDE, LOC, units='m').build()
        assert graph is not None and graph.unit == 'm'
        assert max(json.loads(graph.json)['views']['day']['samples']) < 4.0


# A harmonics file whose stations are stored in METERS.  Flater's free file
# is US-only and all in feet, so without this nothing real ever exercises a
# metric station.  From openwatersio/tide-database's releases; installed in
# its own directory, never production's, so production's station list is
# unchanged.  Missing is a FAILURE, like a missing tide.
METRIC_HFILE_GLOB = os.environ.get('XTIDE_METRIC_HFILE',
                                   '/usr/local/share/xtide-neaps/neaps-*-metric.tcd')
METRIC_LOC = 'Brest, Brittany, France'


class TestRealMetricHarmonics:
    @pytest.fixture
    def metric_hfile(self, monkeypatch):
        import glob
        found = sorted(glob.glob(METRIC_HFILE_GLOB))
        assert found, (
            'no metric harmonics file at %s -- download neaps-<date>-metric.tcd '
            'from https://github.com/openwatersio/tide-database/releases into '
            '/usr/local/share/xtide-neaps/ (or set XTIDE_METRIC_HFILE)' % METRIC_HFILE_GLOB)
        monkeypatch.setenv('HFILE_PATH', found[-1])
        return found[-1]

    def test_events_are_stored_in_meters(self, metric_hfile):
        cfg = xtide.Configuration(lock=threading.Lock(), location=METRIC_LOC,
                                  prog=REAL_TIDE, days=2, events=[])
        assert xtide.XTidePoller.populate_tidal_events(cfg) and cfg.events, \
            '%s not found in %s' % (METRIC_LOC, metric_hfile)
        for ev in cfg.events:
            assert ev.location == METRIC_LOC
            assert ev.usUnits == weewx.METRIC
            assert -1.0 < ev.level < 10.0   # Brest spans about 0.5 to 7.5 m

    @pytest.mark.parametrize('units, unit, top', [('x', 'm', 10.0), ('m', 'm', 10.0), ('ft', 'ft', 30.0)])
    def test_graph_units(self, metric_hfile, units, unit, top):
        graph = xtide.XTideGraphBuilder(REAL_TIDE, METRIC_LOC, units=units).build()
        assert graph is not None
        assert graph.unit == unit
        # No Credit line in this file: the page credits its Source instead.
        assert graph.credit == 'TICON-4'
        samples = json.loads(graph.json)['views']['day']['samples']
        assert max(samples) < top
        if unit == 'ft':
            assert max(samples) > 10.0      # really converted, not relabeled


class TestServiceHelpers:
    def test_events_compare_equal(self):
        ev = xtide.Event(dateTime=1, usUnits=weewx.US, location=LOC,
                         eventType=xtide.EventType.HIGH_TIDE, level=5.0)
        row = {'dateTime': 1, 'usUnits': weewx.US, 'location': LOC,
               'eventType': xtide.EventType.HIGH_TIDE, 'level': 5.0}
        assert xtide.XTide.events_compare_equal([ev], [row])
        assert not xtide.XTide.events_compare_equal([ev], [dict(row, level=5.1)])
        assert not xtide.XTide.events_compare_equal([ev], [])

    def test_time_to_next_poll_is_before_midnight(self):
        secs = xtide.XTidePoller.time_to_next_poll()
        assert 0 < secs <= 86400 + 3600  # DST slack


class TestShutdownPassthrough:
    """weewxd stops by raising Terminate from its SIGTERM handler inside
    whatever the main thread is executing -- here the END_ARCHIVE_PERIOD
    save.  The broad handlers on that path must hand Terminate back
    (recognized by NAME: weewxd runs as __main__, so the real class cannot
    be imported) while still eating ordinary errors."""

    class Terminate(Exception):
        """Same name as weewxd's shutdown exception; the name is all that
        reraise_if_terminate can (and does) match."""

    @staticmethod
    def make_service(raiser):
        # Skip __init__: saveEventsToDB needs only cfg and select_events,
        # which is the first callee inside its try block.
        svc = xtide.XTide.__new__(xtide.XTide)
        svc.cfg = xtide.Configuration(
            lock=threading.Lock(), location=LOC, prog='tide', days=7,
            events=[xtide.Event(dateTime=1, usUnits=weewx.US, location=LOC,
                                eventType=xtide.EventType.HIGH_TIDE, level=5.0)])
        svc.select_events = raiser
        return svc

    def test_terminate_escapes_save_events(self):
        def boom(*args, **kwargs):
            raise TestShutdownPassthrough.Terminate('shutdown')
        with pytest.raises(TestShutdownPassthrough.Terminate):
            self.make_service(boom).saveEventsToDB()

    def test_ordinary_exception_still_swallowed(self, caplog):
        def boom(*args, **kwargs):
            raise RuntimeError('db exploded')
        self.make_service(boom).saveEventsToDB()  # must not raise
        assert 'db exploded' in caplog.text

    def test_terminate_escapes_fetch_records(self):
        # fetch_records is also on the main-thread path (via select_events);
        # Terminate must escape immediately, before any locked-database retry.
        class FakeDbm:
            @staticmethod
            def genSql(select):
                raise TestShutdownPassthrough.Terminate('shutdown')
        with pytest.raises(TestShutdownPassthrough.Terminate):
            xtide.XTideVariables.fetch_records(FakeDbm())


class TestUnquotedLocation:
    """A station name written into weewx.conf without quotes.  Its commas
    make ConfigObj hand back a LIST, which used to reach tide's -l argument
    and raise a TypeError inside the startup fetch -- so weewx would not
    start, with a traceback that never said 'location'.  The README showed
    the value unquoted for years, so the affected stations are the ones
    that followed it."""

    @staticmethod
    def unquoted_conf():
        """[XTide] exactly as an unquoted location parses."""
        text = '[XTide]\n    location = %s\n' % LOC
        parsed = configobj.ConfigObj(io.StringIO(text), encoding='utf-8')
        # Guard the premise: if ConfigObj ever stops splitting on commas,
        # this whole class is testing nothing.
        assert isinstance(parsed['XTide']['location'], list)
        return parsed['XTide']

    def test_a_list_is_rejoined(self, caplog):
        caplog.set_level(logging.INFO)
        assert xtide.config_location(self.unquoted_conf()) == LOC
        # Silent unless the caller asks: graph() runs this every reporting
        # cycle, and logging there would repeat the line for ever.
        assert caplog.text == ''
        assert xtide.config_location(self.unquoted_conf(),
                                     log_unquoted=True) == LOC
        assert 'unquoted' in caplog.text

    def test_a_quoted_location_is_untouched(self, caplog):
        caplog.set_level(logging.INFO)
        assert xtide.config_location({'location': LOC}, log_unquoted=True) == LOC
        assert caplog.text == ''

    def test_absent_location_is_still_none(self):
        # None is what XTide.__init__ and graph() test for; a rejoin that
        # turned it into '' would defeat both.
        assert xtide.config_location({}) is None

    def test_the_service_starts_with_an_unquoted_location(self, make_tide):
        """The bug's real shape: the fetch in XTide.__init__ is synchronous,
        so the TypeError escaped it and weewx never started.  The fetch is
        REAL here (a fake tide program): mocking it away, as the installer
        tests do, would leave this passing with the fix removed."""
        xtide_dict = dict(self.unquoted_conf(), prog=make_tide(SIMULATOR),
                          days=2)
        engine = mock.Mock()
        dbm = engine.db_binder.get_manager.return_value
        dbm.connection.columnsOf.return_value = [c[0] for c in xtide.schema['table']]
        config_dict = {'XTide': xtide_dict, 'DataBindings': {}, 'Databases': {}}
        with mock.patch.object(weewx.manager, 'get_manager_dict',
                               return_value={'schema': xtide.schema}), \
             mock.patch.object(xtide.XTidePoller, 'poll_xtide'):
            service = xtide.XTide(engine, config_dict)
        assert service.cfg.location == LOC
        assert service.cfg.events, 'tide was never run'

    def test_the_graph_gets_a_string(self, make_tide, caplog):
        """graph() reads location straight out of config_dict too, so it
        had the same fault and needs the same fix -- silently: a fresh
        XTideVariables is built every reporting cycle, so a log line here
        would repeat for as long as the station runs."""
        caplog.set_level(logging.INFO)
        prog = make_tide(SIMULATOR)
        generator = mock.Mock()
        generator.config_dict = {'XTide': dict(self.unquoted_conf(),
                                               prog=str(prog))}
        generator.skin_dict = {'Texts': {}}
        generator.converter = weewx.units.Converter()
        variables = xtide.XTideVariables.__new__(xtide.XTideVariables)
        variables.generator = generator
        variables._graph = None
        variables._graph_built = False
        graph = variables.graph()
        assert graph is not None, 'tide was never run: location was not a string'
        assert 'unquoted' not in caplog.text, 'the report path must not log'


class TestInstallerConfig:
    """install.py's config stanza.  It is read exactly once in a station's
    life, by a fresh `weectl extension install`: weecfg merges it with
    weeutil.config.conditional_merge, which fills in absent keys only and
    NEVER rewrites one that is already there.  So a wrong value here ships
    silently and no later release can correct it -- which is why every
    option the extension can answer for itself is written COMMENTED OUT,
    leaving xtide.py's own fallback to govern -- and leaving a better
    default in some later release free to reach every existing station."""

    # Live in [XTide], pinned as a COMPLETE SET below rather than by
    # checking today's commented options are absent: a future release that
    # adds a new option LIVE against a fallback in xtide.py is the very
    # drift this scheme exists to stop, and a named-absence check would
    # not see it.  Adding a live key has to be a deliberate edit here.
    LIVE_XTIDE_OPTIONS = ['data_binding', 'location', 'prog']

    # A commented-out assignment ('#days = 7'), never a prose comment,
    # which always has a space after the '#'.
    COMMENTED_OPTION_RE = re.compile(r'^(\s*)#(\w+)\s*=\s*(.+?)\s*$')
    SECTION_RE = re.compile(r'^\s*(\[+)([^\]]+)\]+\s*$')

    # A weewx.conf that has no [XTide] but DOES have the other three
    # sections the stanza contributes to -- the shape of every real
    # station, and the only shape in which the drop below is visible.
    # Parsed from text rather than built empty: ConfigObj takes its
    # indent_type from what it read, and a bare ConfigObj() writes flush
    # left, which would make the indentation assertion meaningless.
    TARGET_CONF = """[Station]
    location = home
[DataBindings]
    [[wx_binding]]
        database = archive_sqlite
[Databases]
    [[archive_sqlite]]
        database_name = weewx.sdb
[StdReport]
    [[SeasonsReport]]
        skin = Seasons
"""

    @staticmethod
    def install_module():
        """install.py, loaded as a module.  Loading it needs
        weecfg.extension imported first: that module aliases itself in
        sys.modules as 'setup' for installers written against the pre-5.0
        name, which is what install.py's own import resolves through."""
        importlib.import_module('weecfg.extension')  # registers the alias
        spec = importlib.util.spec_from_file_location(
            'xtide_install', os.path.join(REPO, 'install.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @classmethod
    def installer_config(cls):
        return cls.install_module().XTideInstaller()['config']

    @classmethod
    def commented_options(cls):
        """install.py's commented-out assignments, as {section: {option:
        value}}.  Read out of CONFIG as TEXT because a commented-out option
        is by definition absent from the parsed object -- any test that
        walked the parsed stanza would quietly stop covering it."""
        found = {}
        section = None
        for line in cls.install_module().CONFIG.splitlines():
            header = cls.SECTION_RE.match(line)
            if header:
                section = header.group(2).strip()
                continue
            option = cls.COMMENTED_OPTION_RE.match(line)
            if option:
                found.setdefault(section, {})[option.group(2)] = option.group(3)
        return found

    @staticmethod
    def days_the_service_uses(xtide_dict):
        """What XTide actually runs with, given this [XTide] section.  The
        fallback is applied inline in XTide.__init__, so reading it takes a
        started service: the engine, the manager dict and the poller's work
        are mocked away, leaving the config handling itself real.  The seam
        is poll_xtide rather than threading.Thread -- xtide.threading IS the
        stdlib module, so patching Thread there would hand a Mock to any
        other code that started a thread in the same window.  A real daemon
        thread starts here and exits at once on the mocked target."""
        engine = mock.Mock()
        dbm = engine.db_binder.get_manager.return_value
        dbm.connection.columnsOf.return_value = [c[0] for c in xtide.schema['table']]
        config_dict = {'XTide': xtide_dict, 'DataBindings': {}, 'Databases': {}}
        with mock.patch.object(weewx.manager, 'get_manager_dict',
                               return_value={'schema': xtide.schema}), \
             mock.patch.object(xtide.XTidePoller, 'populate_tidal_events'), \
             mock.patch.object(xtide.XTidePoller, 'poll_xtide'):
            service = xtide.XTide(engine, config_dict)
        return service.cfg.days

    def test_version_is_in_lockstep(self):
        """The version lives in three places and they must agree:
        install.py's version=, WEEWX_XTIDE_VERSION in xtide.py, and
        [Extras] version in skins/xtide/skin.conf.  A release that bumps
        two of the three ships a skin reporting the wrong version, which
        nothing else would catch."""
        installer_version = self.install_module().XTideInstaller()['version']
        skin = configobj.ConfigObj(
            os.path.join(REPO, 'skins', 'xtide', 'skin.conf'),
            encoding='utf-8', file_error=True)
        assert installer_version == xtide.WEEWX_XTIDE_VERSION
        assert skin['Extras']['version'] == xtide.WEEWX_XTIDE_VERSION

    def test_html_root_is_a_bare_subdirectory(self):
        """HTML_ROOT must NOT carry a public_html prefix: weecfg prepends
        the installation's own StdReport HTML_ROOT at install time
        (ExtensionEngine.install_config -> prepend_path), so 'xtide'
        becomes public_html/xtide -- or whatever that installation uses.
        'public_html/xtide' here would land the report in
        public_html/public_html/xtide."""
        report = self.installer_config()['StdReport']['XTideReport']
        assert report['HTML_ROOT'] == 'xtide'
        assert report['skin'] == 'xtide'
        # The sample report is meant to render without being turned on.
        assert weeutil.weeutil.to_bool(report['enable'])

    def test_live_options_are_pinned_as_a_complete_set(self):
        xtide_section = self.installer_config()['XTide']
        # .scalars is ConfigObj's list of a section's non-section keys, so
        # this is the complete set, not a spot check.
        assert sorted(xtide_section.scalars) == self.LIVE_XTIDE_OPTIONS
        assert xtide_section['data_binding'] == 'xtide_binding'
        assert xtide_section['prog'] == '/usr/bin/tide'
        # The binding and database the stanza also seeds, which
        # data_binding names.
        binding = self.installer_config()['DataBindings']['xtide_binding']
        assert binding['database'] == 'xtide_sqlite'
        assert binding['schema'] == 'user.xtide.schema'
        assert (self.installer_config()['Databases']['xtide_sqlite']
                ['database_name'] == 'xtide.sdb')

    def test_location_is_a_string_not_a_list(self):
        """location MUST stay quoted in CONFIG.  Its value has commas in
        it, and ConfigObj reads an unquoted comma-separated value as a
        LIST -- which weewx.conf would then carry as a list, and xtide.py
        would hand to tide's -l as something that is not a station name.
        The dict this stanza replaced could not hit this; the text form
        can, silently."""
        location = self.installer_config()['XTide']['location']
        assert isinstance(location, str), location
        assert location == LOC

    def test_placeholders_are_marked(self):
        """Both live values a user has to look at are deliberately not
        answers: location names a station in California and prog names a
        path an XTide built per the README does not use.  Three kinds of
        line share this stanza -- a commented-out assignment (uncomment
        only to pin it), a live setting that means what it says
        (data_binding), and a live setting whose value is a stand-in --
        and only the last kind breaks the extension if it is ignored,
        while looking exactly like a working setting.  The marker leads
        the comment rather than trailing the prose.  weewx-purple and
        weewx-celestial mark theirs the same way."""
        xtide_section = self.installer_config()['XTide']
        for option in ('location', 'prog'):
            # ConfigObj hands back the comment block attached to the key.
            comment = ' '.join(xtide_section.comments[option])
            assert 'PLACEHOLDER' in comment, option

    def test_commented_option_matches_the_fallback_that_governs(self):
        """The drift guard.  A commented-out option shows the user the
        value that will actually be used, so it must equal what xtide.py
        falls back to when the key is absent -- and once the installer
        stops writing it live, nothing but xtide.py governs it.

        WHICH SIDE MOVES WHEN THIS FAILS IS A JUDGEMENT, NOT A FORMALITY.
        Do not make it pass by editing the assignment down to the code.
        While the option was written live, the installer's value is what
        every fresh install has actually been running and the fallback was
        never reached, so editing the assignment turns the test green
        while silently changing what new stations get.  Moving the
        fallback is usually what preserves behavior; moving the assignment
        is a deliberate change of default and belongs in changes.txt.
        (This repo shipped days = 7 against a fallback of 14 for three
        releases; the fallback is the side that moved.)"""
        commented = dict(self.commented_options()['XTide'])
        assert weeutil.weeutil.to_int(commented.pop('days')) == \
            self.days_the_service_uses({'location': LOC})
        # Anything else commented out here is a default nothing checks.
        assert commented == {}

    def test_merged_stanza_keeps_its_commented_option(self):
        """The placement rule, checked through the real merge.  ConfigObj
        attaches a comment block to the NEXT key, and conditional_merge
        transfers a key's comments ONLY when it creates that key -- so a
        commented-out option left last in [XTide] attaches to the
        following top-level [DataBindings], which every real weewx.conf
        already has, and is not merely re-indented but DROPPED, leaving no
        line at all.  Hence prog last in [XTide].

        The count assertion is what catches that: an indentation check
        cannot see a block that is gone.  The indent check catches the
        other failure, where the block survives at the wrong depth and
        reads as an option of the wrong section."""
        merged = configobj.ConfigObj(io.StringIO(self.TARGET_CONF),
                                     encoding='utf-8')
        weeutil.config.conditional_merge(merged, self.installer_config())
        out = io.BytesIO()
        merged.write(out)

        depth = 0
        seen = 0
        for line in out.getvalue().decode('utf-8').splitlines():
            header = self.SECTION_RE.match(line)
            if header:
                depth = len(header.group(1))
                continue
            option = self.COMMENTED_OPTION_RE.match(line)
            if option:
                seen += 1
                assert len(option.group(1)) == 4 * depth, (
                    'wrong indentation, so it merged outside its section: %r'
                    % line)
        # days, and only days.  If this ever counts zero the stanza still
        # merges cleanly and the user simply never sees the option.
        assert seen == 1
