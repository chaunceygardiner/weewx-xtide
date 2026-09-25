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
import math
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
            for frame in xtide.FRAMES:
                scale = view['scales'][frame.name]
                assert scale['vlo'] < min(view['samples'])
                assert scale['vhi'] > max(view['samples'])

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
        for svg in (graph.svg_day, graph.svg_week, graph.svg_month,
                    graph.svg_narrow_day, graph.svg_narrow_week, graph.svg_narrow_month):
            for cls in ('xg-curve', 'xg-night', 'xg-nowline', 'xg-frame', 'xg-hi', 'xg-lo'):
                assert cls in svg, 'missing %s' % cls
        assert 'xg-evlab' in graph.svg_day        # labels on the day view only
        assert 'xg-evlab' not in graph.svg_month
        # The halo paints its stroke under the glyphs only with this
        # attribute; xtide.css cannot say it (the Nu CSS checker rejects it).
        labels = re.findall(r'<text class="xg-lab xg-evlab"[^>]*>', graph.svg_day)
        assert labels and all('paint-order="stroke"' in t for t in labels)
        assert 'paint-order' not in re.sub(r'/\*.*?\*/', '', open(CSS_PATH).read(), flags=re.S)

    def test_event_rows_and_axes_are_unpadded_and_yearless(self, make_tide, monkeypatch):
        """3.2's default: no year on a row of a 30-day table, and no
        zero-padded day or 12-hour hour anywhere.  Before the sweep the same
        page wrote "3:06 PM" on the graph (the SVG path lstrips the zero) and
        "Mon, Sep 14, 2026 03:06 PM" in the table beneath it, for the same
        instant -- one rule applied at one site and not the other."""
        monkeypatch.setattr(xtide, 'use_12_hour_labels', lambda: True)
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        assert g is not None
        for ev in g.events:
            assert re.fullmatch(r'[A-Za-z]{3}, [A-Za-z]{3} [1-9]\d?, [1-9]\d?:\d{2} [AP]M',
                                ev['time_str']), ev['time_str']
        monkeypatch.setattr(xtide, 'use_12_hour_labels', lambda: False)
        g24 = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        assert g24 is not None
        for ev in g24.events:
            # The 24-hour clock keeps its pad; only the day loses one.
            assert re.fullmatch(r'[A-Za-z]{3}, [A-Za-z]{3} [1-9]\d?, \d{2}:\d{2}',
                                ev['time_str']), ev['time_str']
        # The axes the table sits under, swept in the same pass.
        for svg in (g.svg_week, g.svg_month):
            for label in re.findall(r'<text class="xg-lab xg-xlab"[^>]*>([^<]+)</text>', svg):
                assert re.fullmatch(r'[A-Za-z]{3} [1-9]\d?', label), label

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
        svg, _, _ = builder.build_view_svg(xtide.WIDE, 'day', t0, t0 + 2 * 86400, 360,
                                           values, tides, [], 'ft')
        pairs = re.findall(r'<circle class="xg-(?:hi|lo)" cx="[\d.]+" cy="([\d.]+)" r="([\d.]+)"/>'
                           r'<text class="xg-lab xg-evlab" x="[\d.]+" y="([\d.]+)"[^>]*>', svg)
        assert len(pairs) == 4
        top_edge = xtide.WIDE.mt
        bottom_edge = xtide.WIDE.h - xtide.WIDE.mb
        for cy, r, ly in ((float(a), float(b), float(c)) for a, b, c in pairs):
            text_top, text_bottom = ly - 9, ly + 2
            assert text_bottom < cy - r or text_top > cy + r, \
                'label at y=%.1f covers its marker at y=%.1f' % (ly, cy)
            assert top_edge <= text_top and text_bottom <= bottom_edge


# What the narrow frame's type actually measures, in viewBox units, in the
# fallback sans face (DejaVu Sans) at the 16 units that frame is laid out
# for.  MEASURED, not estimated: getComputedTextLength in Chromium 141 and
# Firefox 151 agree to a hundredth ('Mon 14' 59.19 / 59.18, '12 PM' 48.91,
# 'Sep 14' 55.61, '-12.5' 41.41), and the em box rises 15 above the
# baseline and drops 4 below it.  The widest-label figures are rounded up
# and allow for the wider letters another date lands on ('Wed 30' is 60.4
# against 'Mon 14').
#
# THE TIME LABELS ARE NOT ENGLISH.  %a and %b come from the weewxd PROCESS
# locale rather than the lang file, so the figures below are the widest
# over every weekday and month of the locales the shipped translations
# imply, not over English: 'mars 30' 65.75, 'sam. 27' 64.27, '12 p.\u202fm.'
# 64.56, 'sam.' 38.81.  Sizing the clamps from English alone clipped the
# last 30-day label on a French station.
#
# PAIR is the least spacing two neighbors may be centered at -- the sum of
# their half-widths, taken over the widest pair that view can actually put
# side by side.  On the 2-day view that is a time against a weekday, never
# two times, because its ticks alternate.  END is the widest label the
# first or last tick can carry, which is what the clamps must keep inside
# the frame.  tools/verify_page.py re-measures the labels actually drawn,
# in a real browser, and is the oracle that keeps these honest.
NARROW_ASCENT = 15.0
NARROW_DESCENT = 4.0
# The same box as a fraction of the type size, rounded outward, so the
# frames can be checked at whatever size each is drawn for: 14/15 and 15/16
# measured, 11.2 and 3.2 at the wide frame's 12.
ASCENT_PER_UNIT = 0.94
DESCENT_PER_UNIT = 0.27
# curve_points emits two vertices per bucket, plus an anchor at each end
# when index 0 and index n-1 are not themselves bucket extremes.  ONE
# source of truth: written a vertex tighter, the suite stays green on
# today's tide and fails on some future day's.
MAX_THINNED_VERTICES = 2 * (320 // 2) + 2

WIDEST_NARROW_YLAB = 41.5
# Stated at 16 units and scaled by frame.lab / 16 for any other frame:
# advance width is linear in font size ('-12.5' is 38.81 at 15 and 41.41 at
# 16), so one set of measurements serves both drawings.
WIDEST_XLAB_PAIR = {'day': 52.0, 'week': 65.0, 'month': 66.0}
WIDEST_XLAB_END = {'day': 39.0, 'week': 65.0, 'month': 66.0}


class TestNarrowFrame:
    """The second drawing, added in 3.3.

    SVG text is in viewBox units, so the 1000-unit wide drawing shown 334 px
    across on a phone renders its 12-unit labels at 4.0 px.  Enlarging the
    type in the stylesheet was the old answer and it cannot work -- the
    gutter and the foot do not grow with the words -- so the builder draws
    every view a second time into a frame laid out for that width.  These
    tests hold the two frames apart: the wide one frozen, the narrow one
    legible and uncluttered.
    """

    @pytest.fixture
    def graph(self, make_tide):
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        assert g is not None
        return g

    def test_the_wide_frame_is_pinned(self):
        """Every number of the wide drawing, pinned.  weewx-tempestas' tides
        page restyles these SVGs and positions against this geometry, so a
        change here is a change to a published contract and has to be a
        deliberate act rather than a side effect of laying out the other
        frame.

        3.3 makes exactly one: xlab_pad_right 24 -> 26, which moves the last
        time label of each view two units left and touches nothing else (an
        element-by-element diff against the previous release changes three
        elements in three views, all of them that label).  It fixes a clip
        that predates 3.3 -- %b comes from the weewxd process locale, and a
        French station's 'mars 30' reached 1000.66 in a 1000-unit frame."""
        w = xtide.WIDE
        assert (w.w, w.h, w.ml, w.mr, w.mt, w.mb) == (1000, 380, 56, 16, 16, 36)
        assert (w.pw, w.ph) == (928, 328)
        assert (w.ylab_dx, w.ylab_dy, w.xlab_dy) == (8, 4, 18)
        assert w.xlab_pad_left == 20
        assert w.xlab_pad_right == {'day': 26, 'week': 26, 'month': 26}
        assert (w.unitlab_dx, w.unitlab_dy) == (8, 16)
        assert (w.ytick_budget, w.tick_stride, w.max_vertices) == (8, 1, 0)
        assert w.radius == {'day': 4.5, 'week': 3.5, 'month': 2.5}
        assert w.event_labels is True
        assert w.svg_class == 'xg'           # and no xg-narrow on it
        assert w.cursor_r == 5

    def test_the_narrow_frame_clears_the_type_floor(self):
        """11 px on the glass is the floor these pages hold to.  A frame n
        units wide shown p px wide renders k-unit type at k * p / n px, so
        the frame's width is what decides whether its labels can be read --
        checked here at the two phone widths that matter, against the space
        a phone actually gives the graph (the card's padding taken off)."""
        n = xtide.NARROW
        assert n.w == 360
        assert n.svg_class == 'xg xg-narrow'
        # The width the sample skin's own card leaves the graph, which is
        # narrower than the pages that embed it give: 320 and 390 px less
        # the page's 16 px gutters and the card's 14 px padding.
        for screen, shown in ((320, 260), (390, 330)):
            px = n.lab * shown / n.w
            assert px >= 11.0, 'labels are %.1f px on a %d px screen' % (px, screen)
        # What the stylesheet used to do instead, for the record: the wide
        # frame with the largest type its gutter could take, in that space.
        assert 22 * 330 / xtide.WIDE.w < 11.0

    def test_the_narrow_frame_has_room_for_what_it_draws(self):
        """The gutter, the foot, the plot and the two label bands, each
        against the type it has to hold."""
        n = xtide.NARROW
        box = NARROW_ASCENT + NARROW_DESCENT
        # A level label is right-anchored ylab_dx left of the axis, so the
        # widest one must still start inside the frame.
        assert n.ml - n.ylab_dx - WIDEST_NARROW_YLAB >= 0
        # The foot takes a time label's whole box, descender included.
        assert n.mb >= n.xlab_dy + NARROW_DESCENT
        # THE TWO BANDS DO NOT MEET.  The bottom level label and the first
        # time label share the frame's lower left corner; separating them
        # by depth here is what lets xlab_pad_left be 0, which in turn is
        # what leaves the 7-day view's first two labels a full gap apart.
        bottom_ylab_box = n.mt + n.ph + n.ylab_dy + NARROW_DESCENT
        assert n.mt + n.ph + n.xlab_dy - NARROW_ASCENT >= bottom_ylab_box
        # A plot deeper than a third of its width, so that the most level
        # labels the scale can spend -- four ticks plus a pad at each end --
        # still stand a full label-box apart.
        assert n.ph > n.pw / 3
        assert n.ph / (n.ytick_budget + 2) >= box

    @pytest.mark.parametrize('frame', xtide.FRAMES, ids=lambda f: f.name)
    def test_no_label_band_reaches_outside_its_frame(self, frame):
        """Each of the four edges, for EVERY frame -- the property, not the
        instance that found it.  The narrow frame was laid out by sizing the
        gutter, the foot and the right-hand clamp, and its head was left at
        the padding the wide frame happened to use; the top level label sits
        ON the top gridline, so its box rose two units above the frame and
        Chromium clipped it.  A rule that holds on three edges of one frame
        is not a rule."""
        ascent = frame.lab * ASCENT_PER_UNIT
        descent = frame.lab * DESCENT_PER_UNIT
        # The top level label is centered on the top gridline, at mt.
        assert frame.mt + frame.ylab_dy - ascent >= 0, 'the top level label clips'
        # The bottom one, and the time labels below it.
        assert frame.mt + frame.ph + frame.ylab_dy + descent <= frame.h
        assert frame.mt + frame.ph + frame.xlab_dy + descent <= frame.h
        assert frame.ml - frame.ylab_dx >= 0
        # A level label runs left from its anchor, so the anchor itself
        # has to be inside the frame; how much room it needs beyond that is
        # the widest-label check above.

    def test_the_stylesheet_and_the_frames_agree_on_the_type_size(self):
        """THE ONE CONTRACT THAT SPANS TWO LANGUAGES.  A frame's gutter,
        foot, head and label clamps are all derived from the type size it is
        laid out for, but the size itself is set in the STYLESHEET -- the
        builder never writes a font-size.  So the number in xtide.css and
        the frame's lab must be the same number, and nothing made them be:
        every other layout test computes from frame.lab, so editing only the
        stylesheet leaves the whole suite green while the labels collide and
        clip.  Only tools/verify_page.py would catch it, and pytest does not
        collect it.

        Reading source text is right here and only here: this is a contract
        between two copies that must agree, in the same family as the
        no-hex-in-the-template rule, not a claim that anything works."""
        css = re.sub(r'/\*.*?\*/', '', open(CSS_PATH).read(), flags=re.S)

        def font_size(selector):
            m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
            assert m, 'no rule for %r in xtide.css' % selector
            decl = re.search(r'font-size\s*:\s*([^;}]+)', m.group(1))
            assert decl, '%r sets no font-size' % selector
            value = decl.group(1).strip()
            # PX, NEVER A RELATIVE UNIT.  These sizes are in viewBox units:
            # the frame's gutter, foot and clamps are laid out for exactly
            # this number, so a rem or an em would let the reader's root
            # font size rewrite a geometry that was measured in glyphs.
            px = re.fullmatch(r'(\d+(?:\.\d+)?)px', value)
            assert px, ('%r is %r; an SVG label size must be an absolute px, '
                        'because the frame geometry is laid out for that '
                        'number of viewBox units' % (selector, value))
            return float(px.group(1))

        # The base rule styles the wide drawing; the narrow one overrides it.
        assert font_size('\n.xg-lab') == xtide.WIDE.lab, (
            'xtide.css draws the wide frame at %gpx but it is laid out for %d'
            % (font_size('\n.xg-lab'), xtide.WIDE.lab))
        assert font_size('svg.xg-narrow .xg-lab') == xtide.NARROW.lab, (
            'xtide.css draws the narrow frame at %gpx but it is laid out for %d'
            % (font_size('svg.xg-narrow .xg-lab'), xtide.NARROW.lab))

    def test_every_view_is_drawn_into_both_frames(self, graph):
        wide = {'day': graph.svg_day, 'week': graph.svg_week, 'month': graph.svg_month}
        narrow = {'day': graph.svg_narrow_day, 'week': graph.svg_narrow_week,
                  'month': graph.svg_narrow_month}
        for view in ('day', 'week', 'month'):
            assert 'class="xg" data-view="%s" viewBox="0 0 1000 380"' % view in wide[view]
            assert ('class="xg xg-narrow" data-view="%s" viewBox="0 0 360 188"' % view
                    in narrow[view])

    def test_the_narrow_frame_thins_what_will_not_fit(self, graph):
        """Fewer time labels, and a curve with only the vertices 304 units
        of plot can resolve.  Both are what makes the bigger type fit."""
        pairs = ((graph.svg_day, graph.svg_narrow_day),
                 (graph.svg_week, graph.svg_narrow_week),
                 (graph.svg_month, graph.svg_narrow_month))
        for wide, narrow in pairs:
            assert (len(re.findall(r'xg-xlab', narrow))
                    == (len(re.findall(r'xg-xlab', wide)) + 1) // 2)
            assert narrow.count(',') < wide.count(',')
        for svg in (graph.svg_narrow_day, graph.svg_narrow_week, graph.svg_narrow_month):
            points = re.search(r'class="xg-curve" points="([^"]*)"', svg).group(1)
            assert xtide.NARROW.max_vertices == 320
            assert len(points.split(' ')) <= MAX_THINNED_VERTICES
        # No inline event labels: there is no room, and the tooltip is a tap
        # away.  The wide day view still has them.
        assert 'xg-evlab' in graph.svg_day
        assert 'xg-evlab' not in graph.svg_narrow_day

    def test_thinning_keeps_every_peak_and_trough(self):
        """THE ALIASING GUARD.  A tide is an oscillation, and the 30-day
        view packs about 58 cycles into the vertex budget -- barely four
        samples a cycle.  Keeping every nth sample there undersamples it:
        the true crest of a cycle falls between the kept points, so the
        curve is drawn short of it and the high-tide marker floats above a
        curve that never reaches it.  That shipped in a render and was
        caught by looking at the picture, not by a test; this is the test
        that should have caught it.

        THE PROPERTY IS PER CYCLE, and it has to be: over 58 cycles the
        stride lands on SOME crest, so the global range and the number of
        crests drawn are both intact under the broken algorithm and prove
        nothing.  Measured on this input, every nth sample draws one cycle
        0.96 ft short of its true crest, on a range of 8 ft.
        """
        # The simulator's own curve, at the 30-day view's own sampling: the
        # real 12.42-hour tide period, one sample an hour for 30 days.
        period = 12.42
        values = [4.0 + 4.0 * math.sin(2 * math.pi * i / period) for i in range(720)]
        kept = xtide.XTideGraphBuilder.curve_points(values, 320)

        for cycle in range(int(len(values) / period)):
            lo = int(cycle * period)
            hi = min(int((cycle + 1) * period) + 1, len(values))
            drawn = [v for i, v in kept if lo <= i < hi]
            assert drawn, 'cycle %d has no vertex at all' % cycle
            assert abs(max(drawn) - max(values[lo:hi])) < 1e-9, \
                'cycle %d is drawn %.3f short of its crest' % (
                    cycle, max(values[lo:hi]) - max(drawn))
            assert abs(min(drawn) - min(values[lo:hi])) < 1e-9, \
                'cycle %d is drawn %.3f above its trough' % (
                    cycle, min(drawn) - min(values[lo:hi]))

        # And the polyline never doubles back on itself.
        indices = [i for i, _ in kept]
        assert indices == sorted(indices)
        assert len(indices) == len(set(indices))

    def test_thinning_can_spend_both_end_anchors(self):
        """The vertex bound is limit + 2, not limit + 1, and this is the
        case that spends it: when index 0 is neither the lowest nor the
        highest sample of its own bucket, and index n-1 likewise, BOTH end
        anchors are inserted on top of two vertices from every bucket.

        It is not hypothetical -- a search over tide-shaped series finds it
        at a period of 12.46 hours, an ordinary one.  It is rare enough that
        four thousand random trials missed it, which is exactly why the
        bound was written a vertex too tight and the suite stayed green."""
        n = 720
        values = [4.0 + 3.0 * math.sin(2 * math.pi * i / 12.42) for i in range(4, 715)]
        # Turning points inside the first and last buckets put both ends
        # strictly between their neighbors.
        values = [5.0, 3.0, 4.0, 6.0] + values + [6.0, 4.0, 3.0, 5.0, 4.5]
        assert len(values) == n
        kept = xtide.XTideGraphBuilder.curve_points(values, 320)
        assert len(kept) == 322, 'expected both anchors, got %d vertices' % len(kept)
        # The bound the render test polices has to admit this render.
        assert len(kept) <= MAX_THINNED_VERTICES
        assert kept[0][0] == 0 and kept[-1][0] == n - 1
        indices = [i for i, _ in kept]
        assert indices == sorted(indices)
        assert len(indices) == len(set(indices))

    def test_thinning_keeps_both_ends_of_the_curve(self):
        """A curve that lost its last vertex would stop short of the frame."""
        points = xtide.XTideGraphBuilder.curve_points
        assert points([1.0, 2.0, 3.0], 0) == [(0, 1.0), (1, 2.0), (2, 3.0)]
        assert points([1.0, 2.0, 3.0], 9) == [(0, 1.0), (1, 2.0), (2, 3.0)]
        for n in (7, 100, 480, 481, 719, 720):
            values = [float(i) for i in range(n)]
            for limit in (0, 4, 5, 320):
                kept = points(values, limit)
                assert kept[0] == (0, 0.0)
                assert kept[-1] == (n - 1, float(n - 1))
                assert [i for i, _ in kept] == sorted(set(i for i, _ in kept))
                if limit:
                    # Two vertices per bucket, plus the two end anchors.
                    assert len(kept) <= limit + 2

    @pytest.mark.parametrize('frame', xtide.FRAMES, ids=lambda f: f.name)
    def test_time_labels_neither_collide_nor_clip(self, graph, frame):
        """BOTH FRAMES.  Adjacent labels are centered at least a whole label
        apart and neither end runs off the frame, at the widest label any
        SHIPPED LOCALE can put there -- %a and %b come from the weewxd
        process locale, not the lang file.  Sizing these from English alone
        clipped the last 30-day label on a French station: by 1.6 units on
        the narrow frame, which 3.3 introduced, and by 0.66 on the wide one,
        which had it all along.  tools/verify_page.py re-measures the real
        glyphs in a browser at release."""
        wide = {'day': graph.svg_day, 'week': graph.svg_week, 'month': graph.svg_month}
        narrow = {'day': graph.svg_narrow_day, 'week': graph.svg_narrow_week,
                  'month': graph.svg_narrow_month}
        svgs = wide if frame is xtide.WIDE else narrow
        n = frame
        for view, svg in svgs.items():
            scale = frame.lab / 16.0
            pair = WIDEST_XLAB_PAIR[view] * scale
            end = WIDEST_XLAB_END[view] * scale
            xs = [float(x) for x in
                  re.findall(r'<text class="xg-lab xg-xlab" x="([\d.]+)"', svg)]
            assert len(xs) >= 4, '%s: only %d labels' % (view, len(xs))
            assert len(set(xs)) == len(xs), '%s: two labels share an x' % view
            for a, b in zip(xs, xs[1:]):
                assert b - a >= pair, \
                    '%s: neighbors %.1f units apart, the widest pair needs %.1f' % (
                        view, b - a, pair)
            # Neither end runs off the frame, at the widest label that end
            # can carry in ANY shipped locale.  The first is free to sit
            # over the level labels' gutter because it is a band below
            # them; test_the_narrow_frame_has_room_for_what_it_draws is
            # what holds that separation.
            assert xs[0] - end / 2 >= 0, '%s: the first label runs off the left' % view
            assert xs[-1] + end / 2 <= n.w, '%s: the last label runs off the right' % view
            # And the clamp itself is sized for that label, not for English.
            assert n.w - n.xlab_pad_right[view] + end / 2 <= n.w

    def test_the_payload_carries_a_frame_for_each_drawing(self, graph):
        """xtide.js measures a tap against the geometry of the drawing it
        landed on, so both geometries have to reach it -- and the value
        scale is per frame too, because a narrow frame spends fewer
        gridlines and so rounds to a different vlo/vhi."""
        payload = json.loads(graph.json)
        assert set(payload['layouts']) == {'wide', 'narrow'}
        for frame in xtide.FRAMES:
            layout = payload['layouts'][frame.name]
            assert layout == {'w': frame.w, 'h': frame.h, 'ml': frame.ml, 'mt': frame.mt,
                              'pw': frame.pw, 'ph': frame.ph, 'cur': frame.cursor_r}
        for view in payload['views'].values():
            assert set(view['scales']) == {'wide', 'narrow'}
            for scale in view['scales'].values():
                assert scale['vlo'] < scale['vhi']
        # The samples are frame-independent and are sent once, not twice.
        assert 'samples' in payload['views']['day']
        assert 'samples' not in payload['views']['day']['scales']['wide']

    def test_a_narrow_tap_lands_on_the_hour_it_points_at(self, graph):
        """The arithmetic xtide.js does, done here against BOTH frames: a
        pointer at a fraction of the plot maps to that fraction of the
        window.  Reading the narrow drawing against the wide frame's
        numbers -- the bug this per-frame payload prevents -- puts the
        cursor under the thumb and reads out a different hour, so the two
        frames are required to agree on the instant and to disagree on the
        pixel."""
        payload = json.loads(graph.json)
        view = payload['views']['day']

        def instant_at(frame_name, fraction):
            layout = payload['layouts'][frame_name]
            sx = layout['ml'] + fraction * layout['pw']
            return view['t0'] + (sx - layout['ml']) * (view['t1'] - view['t0']) / layout['pw']

        for fraction in (0.0, 0.25, 0.5, 0.77, 1.0):
            assert instant_at('wide', fraction) == instant_at('narrow', fraction)
        # And the same fraction is a different place on the two drawings,
        # which is why the frame has to be read off the element.
        wide, narrow = payload['layouts']['wide'], payload['layouts']['narrow']
        assert wide['ml'] + 0.5 * wide['pw'] != narrow['ml'] + 0.5 * narrow['pw']


class TestSkinAssetsAndPayloadVintages:
    """The skin's javascript and its payload are generated by DIFFERENT
    releases whenever a station is mid-upgrade, and 3.3 changed the payload
    shape.  Two mechanisms keep that from breaking a page, and neither was
    here before 3.3:

      - the deployed assets are refreshed on every report run, so a new
        payload is never served to an old script; and
      - the script bails cleanly if it meets a payload older than itself,
        which is what happens when the skin is newer than the extension.

    Both were found by driving the two vintages against each other in a
    browser, after a page that 'degrades' was observed to die instead.
    """

    def test_every_shipped_asset_is_copied_by_the_skin(self):
        """A stylesheet or script that install.py ships but skin.conf never
        copies is simply absent from the rendered page.

        It asserts nothing about WHICH directive, because copy_once is
        right and was nearly changed on a false premise: "once" scopes to
        the first report cycle of each weewxd RUN, not to "only if the file
        is absent" -- reportengine guards the list with `if self.first_run`
        and the copy is a bare shutil.copy, an unconditional overwrite.  So
        a restart, which installing an extension requires, refreshes them.
        copy_always would work too but re-sends all three to FTP and RSYNC
        stations every cycle for ever, because deep_copy_path drops mtime.
        """
        conf = configobj.ConfigObj(os.path.join(REPO, 'skins', 'xtide', 'skin.conf'),
                                   encoding='utf-8')
        copy = conf['CopyGenerator']
        copied = set()
        for key in ('copy_once', 'copy_always'):
            copied.update(f.strip()
                          for f in weeutil.weeutil.option_as_list(copy.get(key, []))
                          if f.strip())
        installed = set()
        for line in open(os.path.join(REPO, 'install.py'), encoding='utf-8'):
            m = re.search(r"'skins/xtide/([^/']+\.(?:css|js))'", line)
            if m:
                installed.add(m.group(1))
        assert installed, 'install.py ships no skin assets?'
        assert installed <= copied, \
            'shipped but never copied to the page: %s' % sorted(installed - copied)

    def test_the_script_bails_on_a_payload_older_than_itself(self, tmp_path):
        """RUNS the shipped xtide.js under node against a pre-3.3 payload,
        which carries 'layout' singular and no frames.  It must return
        without throwing and without reaching for the document: every line
        past the frame lookup would fail, and updateNow() runs at load, so
        an unguarded script takes the tabs and the event list down with it.

        Sabotage-checked by deleting the guard, which throws
        "Cannot read properties of undefined"."""
        harness = tmp_path / 'run.js'
        harness.write_text("""
const fs = require('fs');
let touched = false;
global.window = { XTIDE_DATA: JSON.parse(process.argv[2]) };
global.document = new Proxy({}, { get() { touched = true; return () => null; } });
global.setInterval = function () {};
let threw = null;
try { (0, eval)(fs.readFileSync(process.argv[3], 'utf8')); }
catch (e) { threw = String(e); }
console.log(JSON.stringify({ threw: threw, touched: touched }));
""")
        # A 3.2 payload: 'layout', singular, and per-view vlo/vhi.
        old_payload = json.dumps({
            'unit': 'ft', 'tz': 'America/Los_Angeles', 'hour12': True,
            'layout': {'w': 1000, 'h': 380, 'ml': 56, 'mt': 16, 'pw': 928, 'ph': 328},
            'views': {'day': {'t0': 0, 't1': 172800, 'step': 360, 'vlo': 0, 'vhi': 8,
                              'samples': [1.0, 2.0]}},
            'events': [], 'T': {},
        })
        js = os.path.join(REPO, 'skins', 'xtide', 'xtide.js')
        completed = subprocess.run(['node', str(harness), old_payload, js],
                                   capture_output=True, encoding='utf-8', timeout=30)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result['threw'] is None, 'an old payload threw: %s' % result['threw']
        assert not result['touched'], 'the script went on to build the page anyway'


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


class TestPresentationParams:
    """graph(clock=, unit_label=): the embedding page's typography.

    The parameter names the DECISION the caller made ("this page is
    12-hour"), not a rendering of it, because one clock decision fans out
    into five renderings that want five different shapes: the event row's
    full date and time, the SVG event labels' time of day, the day-view
    axis's bare hour, and -- in the json payload -- xtide.js's tooltip,
    which formats through Intl and cannot be handed a strftime string at
    any price.  Each test below pins the decision reaching all of them.
    """

    def build(self, make_tide, **kw):
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC, **kw).build()
        assert g is not None
        return g

    # ── the default is what it always was (no drift to a caller's taste) ──

    def test_default_defers_to_the_process_locale(self, make_tide, monkeypatch):
        for locale_is_12 in (True, False):
            monkeypatch.setattr(xtide, 'use_12_hour_labels', lambda v=locale_is_12: v)
            assert json.loads(self.build(make_tide).json)['hour12'] is locale_is_12

    def test_no_arguments_renders_what_an_explicit_clock_does(self, make_tide, monkeypatch):
        # The sample skin passes nothing; its bytes must not move because the
        # parameters now exist.
        monkeypatch.setattr(xtide, 'use_12_hour_labels', lambda: True)
        plain = self.build(make_tide)
        explicit = self.build(make_tide, clock=12)
        assert plain.svg_day == explicit.svg_day
        assert plain.events == explicit.events
        assert plain.events[0]['level_str'].endswith(' feet')   # still spelled out

    # ── clock reaches all five sites ──

    @pytest.mark.parametrize('clock,hour12', [(12, True), (24, False),
                                              ('12', True), ('24', False)])
    def test_clock_overrides_the_locale_everywhere(self, make_tide, monkeypatch, clock, hour12):
        # Pin the locale to the OPPOSITE of what is asked for, so any site
        # still reading use_12_hour_labels() fails here.
        monkeypatch.setattr(xtide, 'use_12_hour_labels', lambda: not hour12)
        g = self.build(make_tide, clock=clock)
        assert json.loads(g.json)['hour12'] is hour12            # xtide.js's tooltip
        if hour12:
            assert all(ev['time_str'].endswith((' AM', ' PM')) for ev in g.events)
            assert re.search(r'\d{1,2}:\d{2} [AP]M</text>', g.svg_day)   # event labels
            assert '>6 AM<' in g.svg_day                                 # axis hours
        else:
            assert all(re.search(r'\d{2}:\d{2}$', ev['time_str']) for ev in g.events)
            assert 'AM' not in g.svg_day and 'PM' not in g.svg_day
            assert '>06<' in g.svg_day

    def test_the_payload_clock_is_what_was_RENDERED_not_what_was_asked(self, make_tide, monkeypatch):
        """The decision passes through [Texts] on its way to the page, and
        every shipped translation maps the 12-hour keys to 24-hour forms.  So
        a 12-hour decision on a Danish report renders a 24-HOUR page, and a
        payload carrying the intent instead of the result would put the
        tooltip back out of step with the table -- the very defect clock=
        exists to prevent.  Both routes to a 12-hour decision are pinned: the
        process locale, and an explicit clock=12 from a skin."""
        da = lang_texts('da')
        monkeypatch.setattr(xtide, 'use_12_hour_labels', lambda: True)
        for kw in ({}, {'clock': 12}):
            g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC, da, **kw).build()
            assert g is not None
            assert json.loads(g.json)['hour12'] is False, kw
            # ...and the page really did render 24-hour, so the payload agrees
            # with what a reader sees rather than with what was asked for.
            assert all(re.search(r'\d{2}:\d{2}$', ev['time_str']) for ev in g.events), kw
        # English is unaffected: there the decision and the rendering agree.
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC, clock=12).build()
        assert json.loads(g.json)['hour12'] is True

    def test_a_bad_clock_logs_and_keeps_the_page(self, make_tide, monkeypatch, caplog):
        # A typo in someone's skin.conf must not cost a station its tides.
        monkeypatch.setattr(xtide, 'use_12_hour_labels', lambda: True)
        with caplog.at_level(logging.ERROR):
            g = self.build(make_tide, clock='12h')
        assert 'clock must be 12 or 24' in caplog.text and '12h' in caplog.text
        assert json.loads(g.json)['hour12'] is True      # fell back, did not blank

    # ── unit_label replaces the word, and only the word ──

    def test_unit_label_replaces_the_spelled_out_unit(self, make_tide):
        g = self.build(make_tide, unit_label='ft')
        assert all(re.fullmatch(r'-?\d+\.\d{2} ft', ev['level_str']) for ev in g.events)

    def test_unit_label_strips_the_weewx_leading_space(self, make_tide):
        # $unit.label.altitude is ' ft': WeeWX labels carry a leading space by
        # convention, and this extension's own label dict builds ' ' + word.
        # The join supplies its own space, so a verbatim label would render
        # "7.72  ft" -- which looks nearly right AND still splits on ' ' the
        # way the single-spaced form did, so it would escape eyes and tests
        # alike.  The idiomatic value is the one every caller will pass.
        for label in (' ft', 'ft', ' ft ', '  ft  '):
            g = self.build(make_tide, unit_label=label)
            assert '  ' not in g.events[0]['level_str']
            assert g.events[0]['level_str'].endswith(' ft')

    def test_an_empty_unit_label_leaves_no_trailing_space(self, make_tide):
        # A report whose altitude label is blank would otherwise put "7.72 "
        # on every row -- invisible in a browser, wrong in the string.
        for label in ('', '   '):
            g = self.build(make_tide, unit_label=label)
            assert re.fullmatch(r'-?\d+\.\d{2}', g.events[0]['level_str']), g.events[0]['level_str']

    def test_unit_label_never_touches_the_unit_token(self, make_tide):
        # $g.unit and payload.unit are DATA -- read out of tide's own output,
        # keying the SVG's unit reminder and xtide.js's tooltip, and printed
        # deliberately by consumers.  A presentation label must not reach them.
        g = self.build(make_tide, unit_label=' furlongs')
        assert g.unit == 'ft'
        assert json.loads(g.json)['unit'] == 'ft'
        assert 'Tide (ft)' in g.svg_day

    def test_unit_label_is_escaped_like_a_translation(self, make_tide):
        g = self.build(make_tide, unit_label='<ft>')
        assert g.events[0]['level_str'].endswith(' &lt;ft&gt;')

    def test_default_unit_label_is_still_translated(self, make_tide):
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC, {'feet': 'Fuss'}).build()
        assert g is not None
        assert g.events[0]['level_str'].endswith(' Fuss')

    # ── the tag itself ──

    def test_searchlist_graph_passes_arguments_through_and_caches_per_set(self, make_tide):
        variables = TestUnits.variables(TestUnits.converter('foot'),
                                        prog=make_tide(SIMULATOR))
        twelve = variables.graph(clock=12, unit_label=' ft')
        twentyfour = variables.graph(clock=24)
        assert json.loads(twelve.json)['hour12'] is True
        assert json.loads(twentyfour.json)['hour12'] is False
        assert twelve.events[0]['level_str'].endswith(' ft')
        assert twentyfour.events[0]['level_str'].endswith(' feet')
        # Cached per argument set: tide is not free, and a page may embed the
        # graph twice with different typography.
        assert variables.graph(clock=12, unit_label=' ft') is twelve
        assert variables.graph(clock=24) is twentyfour


def skin_extras():
    """[Extras] from the shipped skin.conf, as WeeWX hands it to a template."""
    conf = configobj.ConfigObj(os.path.join(REPO, 'skins', 'xtide', 'skin.conf'),
                               encoding='utf-8')
    return conf['Extras']


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
                        searchList=[{'xtide': StubXTide(), 'gettext': gettext, 'lang': lang,
                                     'Extras': skin_extras()}])
        return str(tmpl)

    def test_renders_graph_page(self, make_tide):
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        html = self.render(g)
        assert LOC in html
        assert 'id="xg-wrap-day"' in html
        assert 'id="xt-now"' in html
        assert 'var XTIDE_DATA = {' in html
        # Every asset link carries the version, so a browser holding a
        # cached copy from the previous release fetches this one instead.
        version = skin_extras()['version']
        for asset in ('xtide.css', 'xtide.js', 'xtide_now.js'):
            assert '%s?v=%s' % (asset, version) in html, asset
            assert '"%s"' % asset not in html, '%s is linked unversioned' % asset
        # Rows before the render are pre-dimmed; either way one row per event.
        assert len(re.findall(r'<div class="xg-evrow(?: past)?" data-ts="\d+">', html)) == len(g.events)
        assert html.count('class="ev-k ev-hi"') + html.count('class="ev-k ev-lo"') == len(g.events)
        assert 'Simulated harmonics &amp; tests' in html
        assert 'xtide_icons' not in html
        # Both drawings of every view reach the page; the stylesheet picks.
        for view in ('day', 'week', 'month'):
            assert html.count('data-view="%s" viewBox' % view) == 2
        assert html.count('class="xg xg-narrow"') == 3
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
# THE BAR IS THE STRICTER OF TWO MEASURES (2026-09-14; the same standard
# these skins hold elsewhere).  The WCAG 2 ratio is known to overrate some
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

# Dividers and control outlines, each with the ground it sits on.  In the
# dark palette each must score, by APCA, what its light value scores: light
# is the design, and dark is held to it rather than to a bar of its own.
# The card and graph outlines, gridlines and marks are deliberately absent.
LINES = {
    '--fc-hair-2':       '--fc-surface',   # tide-table rows
    '--fc-evhead-rule':  '--fc-surface',   # the table's column-head rule
    '--fc-rule':         '--fc-surface',   # the Right now card's divider
    '--fc-grid-2':       '--fc-surface',   # day-tab outline, on the card outside it
    '--xg-tip-line':     '--fc-surface',   # tooltip outline, over the graph
    '--fc-head-rule':    '--fc-page',      # the page header's rule
    '--fc-foot-rule':    '--fc-page',      # the footer's rule
}
LINE_TOLERANCE = 1.0


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
    The same constants and steps as weewx-tempestas' tools/contrast.py, so
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

    def test_every_dark_line_scores_its_light_twin(self):
        """A divider that measured Lc 0 in dark (the table rows, the tab and
        tooltip outlines) passed every other palette test, because none of
        them looks at lines.  This measures the tokens.  tools/verify_page.py
        measures the lines as a browser draws them, against what is really
        behind each one, so a rule drawn with the wrong token or on another
        ground fails there, where it cannot fail here."""
        light, dark, _ = css_palettes()
        bad = []
        for tok, ground in sorted(LINES.items()):
            want = abs(apca(light[tok], light[ground]))
            got = abs(apca(dark[tok], dark[ground]))
            if abs(got - want) > LINE_TOLERANCE:
                bad.append('%s on %s: light Lc %.1f, dark Lc %.1f' % (tok, ground, want, got))
        assert not bad, '; '.join(bad)

    def test_the_page_check_measures_as_the_suite_does(self):
        """tools/verify_page.py carries its own APCA, because the python
        that has Playwright has neither pytest nor weewx to import this
        file with.  The two copies must agree, on the published fixed points
        and on every line the page check scores."""
        sys.path.insert(0, os.path.join(REPO, 'tools'))
        try:
            import verify_page
        finally:
            sys.path.pop(0)
        light, dark, _ = css_palettes()
        pairs = [('#000000', '#ffffff'), ('#ffffff', '#000000')]
        for palette in (light, dark):
            pairs += [(palette[tok], palette[ground]) for tok, ground in LINES.items()]
        for text, ground in pairs:
            assert abs(verify_page.apca(text, ground) - apca(text, ground)) < 1e-9, (text, ground)
        assert verify_page.LINE_TOLERANCE == LINE_TOLERANCE

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

    def test_no_language_pads_a_human_facing_day(self):
        """The rule 3.2's sweep applied as a class, pinned so it cannot creep
        back in through any language: a day a reader sees is never
        zero-padded.  Eight of the nine translations already wrote %-d before
        the sweep -- English was the outlier -- and a translator reaching for
        %d would reintroduce "Sep 04" on that language's page alone.
        ('%d' is not a substring of '%-d', so this reads exactly as meant.)"""
        for code in LANG_CODES:
            for key, val in lang_texts(code).items():
                if not key.startswith('%'):
                    continue
                assert '%d' not in key, '%s.conf key pads the day: %r' % (code, key)
                assert '%d' not in val, '%s.conf pads the day: %r' % (code, val)
                # A DATED format (one carrying a month or a day) puts its
                # hour mid-string, where build_view_svg's lstrip('0') net
                # cannot reach it.  Such a format must ask for %-I, never a
                # padded %I -- the bare '%I:%M %p' marker key is fine,
                # because there the time starts the string and is stripped.
                if '%b' in val or '%-d' in val:
                    assert '%I' not in val or '%-I' in val, \
                        '%s.conf pads the hour in a dated format: %r' % (code, val)

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
        variables._graphs = {}
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

        WHICH SIDE MOVES WHEN THIS FAILS IS A JUDGMENT, NOT A FORMALITY.
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
