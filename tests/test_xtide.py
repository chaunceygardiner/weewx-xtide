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
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

os.environ['TZ'] = 'America/Los_Angeles'
time.tzset()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'bin', 'user'))

import weewx  # noqa: E402
import xtide  # noqa: E402

LOC = 'Palo Alto Yacht Harbor, San Francisco Bay, California'

# Real tide output, both vintages (from changes.txt / the parser comments).
V216_OUTPUT = '''"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,1:12 AM PDT,8.50 ft,"High Tide"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,5:54 AM PDT,,"Sunrise"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,7:24 AM PDT,,"Moonrise"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,9:31 AM PDT,-0.64 ft,"Low Tide"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,3:41 PM PDT,6.57 ft,"High Tide"
"Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,8:32 PM PDT,,"Sunset"
'''
V215_OUTPUT = '''Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,1:12 AM PDT,8.50 ft,High Tide
Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,5:54 AM PDT,,Sunrise
Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,9:31 AM PDT,-0.64 ft,Low Tide
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
# events for plain mode, honoring -b/-e/-s/-m.  The epoch is recovered from
# the "(NNNNNNNN)" suffix that weeutil.timestamp_to_string appends.
SIMULATOR = '''import datetime, math, re, sys
args = sys.argv[1:]
def get(flag):
    return args[args.index(flag) + 1]
begin = int(re.search(r'\\((\\d+)\\)', get('-b')).group(1))
end = int(re.search(r'\\((\\d+)\\)', get('-e')).group(1))
hh, mm = get('-s').split(':')
step = int(hh) * 3600 + int(mm) * 60
LOC = %r
def level(t):
    return 4.0 + 4.0 * math.sin(2 * math.pi * t / (12.42 * 3600))
def stamp(t):
    dt = datetime.datetime.fromtimestamp(t).astimezone()
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
        print('"%%s",%%s,%%.2f ft,"%%s"' %% (LOC, stamp(t), level(t), kind))
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
        out = '"%s",2024-07-07,1:12 AM PDT,2.59 m,"High Tide"\n' % LOC
        cfg = make_cfg(make_tide(canned(out)))
        assert xtide.XTidePoller.populate_tidal_events(cfg)
        assert cfg.events[0].usUnits == weewx.METRIC
        assert cfg.events[0].level == 2.59


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
        assert first['icon'] in ('high-tide.png', 'low-tide.png')
        assert first['level_str'].endswith(' feet')

    def test_svgs_have_expected_parts(self, graph):
        for svg in (graph.svg_day, graph.svg_week, graph.svg_month):
            for cls in ('xg-curve', 'xg-night', 'xg-nowline', 'xg-frame', 'xg-hi', 'xg-lo'):
                assert cls in svg, 'missing %s' % cls
        assert 'xg-evlab' in graph.svg_day        # labels on the day view only
        assert 'xg-evlab' not in graph.svg_month

    def test_build_fails_gracefully(self, make_tide):
        prog = make_tide(canned(stderr=STATION_NOT_FOUND_STDERR, rc=0))
        assert xtide.XTideGraphBuilder(prog, LOC).build() is None

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


class TestSampleTemplate:
    """End-to-end Cheetah render.  Compilation alone is NOT sufficient: with
    #errorCatcher Echo, failures render as un-substituted placeholders."""

    def render(self, graph):
        from Cheetah.Template import Template
        class StubXTide:
            def graph(self):
                return graph
        tmpl = Template(file=os.path.join(REPO, 'skins', 'xtide', 'index.html.tmpl'),
                        searchList=[{'xtide': StubXTide()}])
        return str(tmpl)

    def test_renders_graph_page(self, make_tide):
        g = xtide.XTideGraphBuilder(make_tide(SIMULATOR), LOC).build()
        html = self.render(g)
        assert LOC in html
        assert 'id="xg-wrap-day"' in html
        assert 'var XTIDE_DATA = {' in html
        assert html.count('class="xg-evrow"') == len(g.events)
        assert '$g' not in html  # no un-substituted placeholders (errorCatcher Echo)

    def test_renders_failure_page(self):
        html = self.render(None)
        assert 'No tidal data to display' in html
        assert 'xg-wrap-day' not in html
        assert '$g' not in html

    def test_no_hex_colors_in_template(self):
        # Cheetah owns '#': colors belong in xtide.css, never in the template.
        text = open(os.path.join(REPO, 'skins', 'xtide', 'index.html.tmpl')).read()
        assert not re.search(r'#[0-9a-fA-F]{6}', text)


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
        from weeutil.weeutil import timestamp_to_string
        out = subprocess.run([REAL_TIDE, '-l', LOC, '-b', timestamp_to_string(begin),
                              '-e', timestamp_to_string(begin + hours * 3600),
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
            datetime.datetime.strptime('%s %s' % (cols[1], cols[2]), '%Y-%m-%d %I:%M %p %Z')

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
