#!/usr/bin/python3
# Copyright 2024-2026 by John A Kline <john@johnkline.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""The xtide extension fetches tide forecasts.

See the README for installation and usage.
"""
import configobj
import csv
import datetime
import html
import json
import locale
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time


from enum import Enum
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import weewx
import weewx.units
import weeutil.weeutil

from weeutil.weeutil import timestamp_to_string
from weeutil.weeutil import to_float
from weeutil.weeutil import to_int
from weewx.engine import StdService
from weewx.cheetahgenerator import SearchList

log = logging.getLogger(__name__)

WEEWX_XTIDE_VERSION = "3.2"

if sys.version_info[0] < 3:
    raise weewx.UnsupportedFeature(
        "weewx-xtide requires Python 3, found %s" % sys.version_info[0])

if weewx.__version__ < "4":
    raise weewx.UnsupportedFeature(
        "WeeWX 4 is required, found %s" % weewx.__version__)


def reraise_if_terminate(e: BaseException) -> None:
    """weewxd stops by raising Terminate from its SIGTERM signal handler --
    inside whatever the main thread is executing at that instant, which here
    is the END_ARCHIVE_PERIOD database save.  Every broad exception handler
    on a main-thread path must call this first and hand the exception back,
    or weewx cannot shut down.  weewxd runs as __main__, so its Terminate
    class cannot be imported here and is recognized by name."""
    if type(e).__name__ == 'Terminate':
        raise e


# Schema for xtide database (xtide.sdb).
table = [
    ('dateTime',  'INTEGER NOT NULL PRIMARY KEY'), # Time of event
    ('usUnits',   'STRING NOT NULL'),              # 1 (weewx.US) or 2 (weewx.METRIC)
    ('location',  'STRING NOT NULL'),              # Location for which event is reported
    ('eventType', 'INTEGER NOT NULL'),             # 1 (HIGH_TIDE) or 2 (LOW_TIDE)
    ('level',     'FLOAT NOT NULL'),               # tide level in feet or meters (depending on usUnits)
    ]

schema = {
    'table'         : table,
}


class EventType(Enum):
    HIGH_TIDE = 1
    LOW_TIDE  = 2
    OTHER     = 3

@dataclass
class Event:
    dateTime : int
    usUnits  : int
    location : str
    eventType: EventType
    level    : float

@dataclass
class Configuration:
    lock     : threading.Lock
    location : str            # Controlled by lock
    prog     : str            # Controlled by lock
    days     : int            # Controlled by lock
    events   : List[Event]    # Controlled by lock


def config_location(xtide_config_dict, log_unquoted: bool = False) -> Optional[str]:
    """The station name from weewx.conf's [XTide], or None if unset.

    A station name almost always contains commas, and weewx.conf reads an
    unquoted comma-separated value as a LIST -- so a hand-written
        location = Palo Alto Yacht Harbor, San Francisco Bay, California
    arrives here as three strings.  Rejoin them: that is plainly what was
    meant, and left alone the list reaches tide's -l argument, where
    subprocess raises a TypeError that never mentions location -- during
    the startup fetch, so weewx does not start at all.

    Only the caller that runs once per weewxd asks for the rejoin to be
    logged: the report path builds a fresh XTideVariables every reporting
    cycle, and logging there would repeat the same line for ever.
    """
    location = xtide_config_dict.get('location', None)
    if isinstance(location, list):
        location = ', '.join(location)
        if log_unquoted:
            log.info('location is unquoted in weewx.conf and was read as a '
                     'list; using \'%s\'.  Quote it to be rid of this '
                     'message.' % location)
    return location


class XTide(StdService):
    """Fetch XTide Forecasts"""
    def __init__(self, engine, config_dict):
        super(XTide, self).__init__(engine, config_dict)
        log.info("Service version is %s." % WEEWX_XTIDE_VERSION)

        self.config_dict = config_dict
        self.xtide_config_dict = config_dict.get('XTide', {})
        self.engine = engine

        # get the database parameters we need to function
        self.data_binding = self.xtide_config_dict.get('data_binding', 'xtide_binding')

        self.dbm_dict = weewx.manager.get_manager_dict(
            self.config_dict['DataBindings'],
            self.config_dict['Databases'],
            self.data_binding)

        # [possibly] initialize the database
        dbmanager = engine.db_binder.get_manager(data_binding=self.data_binding, initialize=True)
        log.info("Using binding '%s' to database '%s'" % (self.data_binding, dbmanager.database_name))

        # Check that schema matches
        dbcol = dbmanager.connection.columnsOf(dbmanager.table_name)
        memcol = [x[0] for x in self.dbm_dict['schema']['table']]
        if dbcol != memcol:
            # raise Exception('xtide schema mismatch: %s != %s' % (dbcol, memcol))
            log.error('You must delete the xtide.sdb database and restart weewx.  It contains an old schema!')
            return

        location    = config_location(self.xtide_config_dict, log_unquoted=True)
        if location is None:
            log.error('location must be specified.')
            return

        self.cfg = Configuration(
            lock        = threading.Lock(),
            location    = location,
            prog        = self.xtide_config_dict.get('prog', '/usr/bin/tide'),
            days = to_int(self.xtide_config_dict.get('days', 7)),
            events      = [],
            )

        XTidePoller.populate_tidal_events(self.cfg)

        log.info('location    : %s' % self.cfg.location)
        log.info('prog        : %s' % self.cfg.prog)
        log.info('days: %s' % self.cfg.days)

        # Start a thread to query tide events.
        xtide_poller: XTidePoller = XTidePoller(self.cfg)
        t_events: threading.Thread = threading.Thread(target=xtide_poller.poll_xtide)
        t_events.name = 'XTide Events'
        t_events.daemon = True
        t_events.start()

        self.bind(weewx.END_ARCHIVE_PERIOD, self.end_archive_period)

    def end_archive_period(self, _event):
        log.debug('end_archive_period: saving tidal events to DB')
        self.saveEventsToDB()

    def select_events(self, max_events: Optional[int] = None) -> List[Dict[str, Any]]:
        dbmanager = self.engine.db_binder.get_manager(self.data_binding)
        return XTideVariables.fetch_records(dbmanager, max_events)

    def saveEventsToDB(self) -> None:
        try:
            log.debug('saveEventsToDB: start')
            with self.cfg.lock:
                if len(self.cfg.events) == 0:
                    return
                # Before deleting exiting events and readding, check if
                # anything has changed.
                events_in_db = self.select_events()
                if XTide.events_compare_equal(self.cfg.events, events_in_db):
                    log.info('Ignoring generated tidal events as they have not changed.')
                    self.cfg.events.clear()
                    return
                # The events have changed, got ahead and delete and re-add.
                self.delete_all_events()
                for event in self.cfg.events:
                    self.save_event(XTide.convert_to_json(event))
                log.info('Saved %d events.' % len(self.cfg.events))
                self.cfg.events.clear()
        except Exception as e:
            reraise_if_terminate(e)
            # Include a stack traceback in the log:
            # but eat this exception as we don't want to bring down weewx
            log.error('saveEventsToDB: %s (%s)' % (e, type(e)))
            weeutil.logger.log_traceback(log.error, "    ****  ")

    def save_event(self, event) -> None:
        """save event to database"""
        dbmanager = self.engine.db_binder.get_manager(self.data_binding)
        dbmanager.addRecord(event)

    def delete_all_events(self) -> None:
        try:
           dbmanager = self.engine.db_binder.get_manager(self.data_binding)
           try:
               select = 'SELECT COUNT(dateTime) FROM archive'
               log.debug('Getting count of events: %s.' % select)
               row = dbmanager.getSql(select)
           except Exception as e:
               reraise_if_terminate(e)
               log.error('delete_all_events: %s failed with %s (%s).' % (select, e, type(e)))
               weeutil.logger.log_traceback(log.error, "    ****  ")
               return
           # If there are events, delete them.
           if row[0] != 0:
               delete = 'DELETE FROM archive'
               dbmanager.getSql(delete)
        except Exception as e:
           reraise_if_terminate(e)
           log.error('delete_all_events: %s failed with %s (%s).' % (delete, e, type(e)))
           weeutil.logger.log_traceback(log.error, "    ****  ")

    @staticmethod
    def events_compare_equal(events: List[Event], db_events: List[Dict[str, Any]]) -> bool:
        if len(events) != len(db_events):
            return False

        for i in range(len(events)):
            if events[i].dateTime != db_events[i]['dateTime'] or events[i].usUnits != db_events[i]['usUnits'] or events[i].location != db_events[i]['location'] or events[i].eventType != db_events[i]['eventType'] or events[i].level != db_events[i]['level']:
                return False

        return True

    @staticmethod
    def convert_to_json(event) -> Dict[str, Any]:
        log.debug('convert_to_json: start')
        j = {}
        j['dateTime']      = event.dateTime
        j['usUnits']       = event.usUnits
        j['location']      = event.location
        j['eventType']     = event.eventType.value
        j['level']         = event.level
        log.debug('convert_to_json: returning: %s' % j)
        return j

class XTidePoller:
    def __init__(self, cfg: Configuration):
        self.cfg             = cfg

    def poll_xtide(self) -> None:
        while True:
            try:
                if XTidePoller.populate_tidal_events(self.cfg):
                    log.info('XTidePoller.populate_tidal_events returned %d tidal events.' % len(self.cfg.events))
                else:
                    log.info('XTidePoller.populate_tidal_events failed')
            except Exception as e:
                log.error('poll_xtide: Encountered exception: %s (%s)' % (e, type(e)))
                weeutil.logger.log_traceback(log.error, "    ****  ")

            sleep_time = XTidePoller.time_to_next_poll()
            log.debug('poll_xtide: Sleeping for %f seconds.' % sleep_time)
            time.sleep(sleep_time)

    @staticmethod
    def time_to_next_poll() -> float:
        # determine the number of seconds until midnight tonight
        now: datetime.datetime = datetime.datetime.now().astimezone()
        midnight_this_morning = now.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_tonight = midnight_this_morning + datetime.timedelta(hours=24)
        return midnight_tonight.timestamp() - time.time()

    @staticmethod
    def populate_tidal_events(cfg: Configuration) -> bool:
        with cfg.lock:
            try:
                # Begin is the start of today (in current timezone)
                now: datetime.datetime = datetime.datetime.now().astimezone()
                begin = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                end: int = to_int(begin + 24 * 3600 * cfg.days)
                # -u x: levels in the harmonics file's own units, whatever a
                # units preference in the WeeWX user's ~/.xtide.xml says, so
                # the database always agrees with the harmonics.  Each row
                # records its units; reports convert on the way out.
                completed = subprocess.run([cfg.prog, '-z', '-u', 'x', '-l', cfg.location, '-b', tide_utc_arg(begin), '-e', tide_utc_arg(end), '-fc', '-m', 'p', '-s', '01:00'], capture_output=True, encoding='utf-8', timeout=10)
                if completed.returncode != 0:
                    log.error("Call to tide failed: loc='%s' rc=%d %s" % (cfg.location, completed.returncode, XTidePoller.extract_tide_error(completed.stderr)))
                    return False
                # tide is run with -z, so all times below are UTC.
                # xtide v2.16
                # "Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,8:12 AM UTC,8.50 ft,"High Tide"
                # "Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,12:54 PM UTC,,"Sunrise"
                # "Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,2:24 PM UTC,,"Moonrise"
                # "Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,4:31 PM UTC,-0.64 ft,"Low Tide"
                # "Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-07,10:41 PM UTC,6.57 ft,"High Tide"
                # "Palo Alto Yacht Harbor, San Francisco Bay, California",2024-07-08,3:32 AM UTC,,"Sunset"
                # xtide v2.15
                # Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,8:12 AM UTC,8.50 ft,High Tide
                # Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,12:54 PM UTC,,Sunrise
                # Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,2:24 PM UTC,,Moonrise
                # Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,4:31 PM UTC,-0.64 ft,Low Tide
                out = []
                for line in completed.stdout.splitlines():
                    cols = next(csv.reader([line]))
                    if len(cols) == 5:
                        out.append(line)
                    else:
                        log.info("ignoring line: %s" % line)
                if out:
                    log.debug('tide returned %d lines.' % len(out))
                    cfg.events = []
                    for line in out:
                        cols = next(csv.reader([line]))
                        eventType  = XTidePoller.encode_event_type(cols[4])
                        if eventType == EventType.HIGH_TIDE or eventType == EventType.LOW_TIDE:
                            unit = cols[3].split(' ')[1]
                            cfg.events.append(Event(
                                dateTime  = tide_event_ts(cols[1], cols[2]), # e.g. 2024-07-07 8:32 PM UTC (tide run with -z)
                                usUnits   = weewx.US if unit == 'ft' else weewx.METRIC,
                                # Older versions of xtide substitute | for , in descriptions
                                # Newer version quote location and this will be a noop.
                                location  = cols[0].replace('|', ','),
                                eventType = eventType,
                                level     = to_float(cols[3].split(' ')[0]),
                            ))
                        else:
                            log.debug('Ignoring %s event: %s' % (cols[4], line))
                    log.debug('Fetched %d events (includes sunrise/sunset events).'  % len(out))
                    return True
                else:
                    # tide exited 0 but produced no usable lines (e.g., unknown station)
                    log.error("tide returned no events: loc='%s' %s" % (cfg.location, XTidePoller.extract_tide_error(completed.stderr)))
                    return False
            except FileNotFoundError:
                log.error('%s not found' % cfg.prog)
                return False
            except subprocess.TimeoutExpired:
                log.error("tide for location %s timed out." % cfg.location)
                return False

    @staticmethod
    def extract_tide_error(stderr: str) -> str:
        """Return tide's error message, skipping the GPL disclaimer banner it prints first."""
        for marker in ('XTide Fatal Error:', 'XTide Error:'):
            idx = stderr.find(marker)
            if idx != -1:
                return stderr[idx:].strip()
        return stderr.strip()

    @staticmethod
    def encode_event_type(event_str: str) -> EventType:
        match event_str:
            case "High Tide":
                return EventType.HIGH_TIDE
            case "Low Tide":
                return EventType.LOW_TIDE
            case _:
                return EventType.OTHER

    @staticmethod
    def event_type_from_int(i: int) -> EventType:
        match i:
            case 1:
                return EventType.HIGH_TIDE
            case 2:
                return EventType.LOW_TIDE
            case _:
                return EventType.OTHER

def local_timezone_name() -> Optional[str]:
    """IANA name of the machine's timezone (from /etc/localtime), or None."""
    try:
        path = os.path.realpath('/etc/localtime')
        if '/zoneinfo/' in path:
            return path.split('/zoneinfo/')[-1]
    except OSError:
        pass
    return None


def tide_utc_arg(ts: float) -> str:
    """Format an epoch as a bare UTC 'YYYY-MM-DD HH:MM' for tide's -b/-e.  tide
    is run with -z, so the query window must be given in UTC; this keeps the
    window correct regardless of the server or tide-station timezone."""
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')


def tide_event_ts(date_str: str, time_str: str) -> int:
    """Convert a tide -z csv event time ('YYYY-MM-DD', 'H:MM AM/PM UTC') to a
    UTC epoch.  tide is run with -z so the time is UTC; force UTC explicitly
    rather than trusting strptime's %Z, which only reliably resolves the
    server's own local zone abbreviations (so a station in another zone would
    otherwise be parsed as local time and be off by the offset).  The AM/PM
    token is matched by hand: tide's csv is always English, but strptime's
    %p only matches the current locale's designators, and most non-English
    locales define those as empty strings, so 'AM' could never match and
    weewx failed to start.  Only numeric strptime directives (locale-safe)
    remain."""
    m = re.fullmatch(r'(\d{1,2}):(\d{2}) (AM|PM) UTC', time_str)
    if m is None or not 1 <= int(m.group(1)) <= 12:
        raise ValueError('unrecognized tide event time: %r' % time_str)
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == 'PM' else 0)
    dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').replace(
        hour=hour, minute=int(m.group(2)), tzinfo=datetime.timezone.utc)
    return to_int(dt.timestamp())


def use_12_hour_labels() -> bool:
    """Whether the sample skin's time labels should be 12-hour AM/PM.
    Locales with empty AM/PM designators (most of continental Europe) would
    render %p as nothing, leaving '9:16' ambiguous -- and those locales are
    24-hour anyway.  English-style locales keep the traditional 12-hour
    labels, unchanged."""
    try:
        return locale.nl_langinfo(locale.AM_STR) != ''
    except AttributeError:  # platform without nl_langinfo
        return True


def resolve_clock(clock: Any) -> bool:
    """Whether to write 12-hour times, for a caller-supplied clock= value.
    None -- the default, and what the sample skin passes -- defers to the
    process locale via use_12_hour_labels(); 12 or 24 states the embedding
    page's own choice.  Accepts the ints and the strings '12'/'24', since
    Cheetah hands a template's arguments across as strings more often than
    anyone expects.  Anything else is a typo in someone's skin.conf: log it
    and fall back, because blanking a station's tide page over a misspelled
    option is out of all proportion to the mistake."""
    if clock is None:
        return use_12_hour_labels()
    try:
        hours = int(clock)
    except (TypeError, ValueError):
        hours = 0
    if hours == 12:
        return True
    if hours == 24:
        return False
    log.error('graph: clock must be 12 or 24, not %r; using the locale default.' % (clock,))
    return use_12_hour_labels()


def tide_units(converter: weewx.units.Converter) -> str:
    """tide's -u value for a report: the report's altitude unit when tide can
    draw in it, else 'x' (the harmonics file's own units)."""
    unit, _ = converter.getTargetUnit('altitude')
    return {'foot': 'ft', 'meter': 'm'}.get(unit, 'x')


class XTideGraph:
    """Everything the sample skin's graph page needs; built by XTideGraphBuilder."""
    def __init__(self, location: str, unit: str, svgs: Dict[str, str], payload: str, events: List[Dict[str, Any]],
                 credit: str = ''):
        self.location  = location
        self.unit      = unit         # 'ft' or 'm'
        self.credit    = credit       # where the station's data come from, markup-escaped; may be ''
        self.svg_day   = svgs['day']
        self.svg_week  = svgs['week']
        self.svg_month = svgs['month']
        self.json      = payload      # javascript data for tabs/tooltip (xtide.js)
        self.events    = events       # display rows for the event list


class XTideGraphBuilder:
    """Builds the sample skin's interactive tide graph at report time by
    running the tide program directly.  Deliberately independent of the
    events database and the days setting: the graph always covers 30 days,
    while the database keeps serving $xtide.events() and external consumers.
    """

    # SVG layout in viewBox units; mirrored to javascript via the json payload.
    W = 1000
    H = 380
    ML = 56   # left margin (level labels)
    MR = 16
    MT = 16
    MB = 36   # bottom margin (time labels)

    # (view, days, sample seconds).  The 'day' view is today plus tomorrow,
    # midnight to midnight, so an evening visitor still sees a full day ahead.
    VIEWS = [
        ('day',    2,  360),
        ('week',   7,  900),
        ('month', 30, 3600),
    ]

    def __init__(self, prog: str, location: str, texts: Optional[Dict[str, Any]] = None,
                 units: str = 'x', clock: Any = None, unit_label: Optional[str] = None):
        self.prog = prog
        self.location = location
        # tide's -u: 'ft' or 'm' to draw in the report's units, 'x' for the
        # harmonics file's own.  Always passed, so a units preference in
        # ~/.xtide.xml cannot change what is drawn -- and so the raw-mode
        # curve, whose lines carry no unit, is in the same units as the
        # plain-mode events the unit is read from.
        self.units = units
        # The report's [Texts] section (skin_dict with the lang file merged
        # in), for the strings this builder composes server-side.
        self.texts: Dict[str, Any] = texts if texts is not None else {}
        # The caller's presentation choices, for a skin embedding the graph
        # in a page with its own typography.  Both default to what the
        # sample skin has always done, so omitting them changes nothing.
        # One decision, one owner: hour12 drives every time this builder
        # writes AND rides the json payload, so xtide.js's tooltip cannot
        # disagree with the labels and rows around it.
        self.hour12 = resolve_clock(clock)
        self.unit_label = unit_label

    # ── translation ──────────────────────────────────────────────────────
    @staticmethod
    def _esc(s: str) -> str:
        """Escape for markup: the three characters that matter in the text
        nodes these strings land in.  Shared so a caller-supplied label is
        escaped exactly as a translation is."""
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def _t(self, key: str, **values: Any) -> str:
        """The [Texts] translation for key (gettext-style: the English
        string IS the key, a missing entry falls back to it), escaped for
        markup, then {name} placeholders filled from values.  Call sites
        always pass the key as a single-line literal: the test suite reads
        them from this source file to enforce that lang/en.conf ships
        exactly the keys that render, in both directions."""
        s = self.texts.get(key, key)
        if not isinstance(s, str):
            s = key
        s = self._esc(s)
        if not values:
            return s
        try:
            return s.format(**values)
        except (KeyError, IndexError, ValueError):
            # A translation with broken placeholders must not blank the
            # page: fall back to the English key, which always formats.
            return key.format(**values)

    def _raw(self, key: str) -> str:
        """The [Texts] translation for key, unescaped: strftime formats
        (the skyfield date pattern -- the format string itself is the key,
        so a language reorders day and month, while the NAMES %a/%b emit
        come from the weewxd process locale) and strings bound for the
        json payload, which land in the page via textContent."""
        s = self.texts.get(key, key)
        return s if isinstance(s, str) else key

    def build(self) -> Optional[XTideGraph]:
        try:
            now: datetime.datetime = datetime.datetime.now().astimezone()
            begin = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            month_end = begin + 30 * 86400
            # One extra day on each side so night shading has the sunset
            # before the window and events at the edges are not clipped.
            parsed = self.get_events(begin - 86400, month_end + 86400)
            if parsed is None:
                return None
            tides, suns, unit = parsed
            svgs: Dict[str, str] = {}
            views: Dict[str, Any] = {}
            for name, days, step in self.VIEWS:
                end = begin + days * 86400
                samples = self.get_samples(begin, end, step)
                if not samples:
                    return None
                t0, actual_step, values = samples
                # Scale to the intended window (t1 on a midnight), not to the
                # last sample, which raw mode stops one step short of.
                t1 = t0 + days * 86400
                view_tides = [ev for ev in tides if t0 <= ev[0] <= t1]
                nights = self.night_intervals(suns, t0, t1)
                svg, vlo, vhi = self.build_view_svg(name, t0, t1, actual_step, values, view_tides, nights, unit)
                svgs[name] = svg
                views[name] = {
                    't0': t0, 't1': t1, 'step': actual_step,
                    'vlo': vlo, 'vhi': vhi,
                    'samples': [round(v, 3) for v in values],
                }
            time_fmt = (self._raw('%a, %b %-d, %-I:%M %p') if self.hour12
                        else self._raw('%a, %b %-d, %H:%M'))
            payload = {
                'unit': unit,
                'tz': local_timezone_name(),
                # The clock for xtide.js's tooltip: without it the tooltip
                # formats via Intl on the VISITOR's locale and can read
                # 24-hour over a 12-hour table.  Read off the RESOLVED
                # format, never self.hour12: the decision passes through
                # [Texts] on its way to the page, and every shipped
                # translation maps the 12-hour keys to 24-hour forms, so a
                # 12-hour decision renders a 24-hour Danish page.  Sending
                # the intent rather than what was rendered put the tooltip
                # back out of step with the table -- the defect this whole
                # mechanism exists to prevent.
                'hour12': '%p' in time_fmt,
                'layout': {'w': self.W, 'h': self.H, 'ml': self.ML, 'mt': self.MT,
                           'pw': self.W - self.ML - self.MR, 'ph': self.H - self.MT - self.MB},
                'views': views,
                'events': [[ev[0], round(ev[1], 3), ev[2]] for ev in tides if begin <= ev[0] <= month_end],
                # Strings xtide.js composes client-side, translated at
                # generation time (json.dumps \u-escapes non-ASCII).
                'T': {
                    'High Tide': self._raw('High Tide'),
                    'Low Tide': self._raw('Low Tide'),
                    '{n} tidal events.': self._raw('{n} tidal events.'),
                    'Rising': self._raw('Rising'),
                    'Falling': self._raw('Falling'),
                },
            }
            # The caller's label wins when it gave one, for a page that
            # writes units short ("7.72 ft").  WeeWX unit labels carry a
            # LEADING SPACE by convention -- $unit.label.altitude is ' ft',
            # and this extension's own label dict builds ' ' + word -- while
            # the join below supplies its own space.  Strip, or the
            # idiomatic caller gets "7.72  ft": a double space that renders
            # nearly right and still splits on ' ' the way the single-spaced
            # form did, so it would escape both eyes and tests.
            if self.unit_label is not None:
                unit_long = self._esc(self.unit_label.strip())
            else:
                unit_long = self._t('feet') if unit == 'ft' else self._t('meters')
            # A blank label (a report whose altitude label is empty) must not
            # leave a trailing space on every row.
            level_fmt = '%.2f %s' if unit_long else '%.2f'
            events_display = []
            for ts, level, event_type in tides:
                if not begin <= ts <= month_end:
                    continue
                high = event_type == EventType.HIGH_TIDE.value
                events_display.append({
                    'ts'       : ts,
                    'eventType': self._t('High Tide') if high else self._t('Low Tide'),
                    'high'     : high,
                    'level_str': level_fmt % ((level, unit_long) if unit_long else level),
                    'time_str' : datetime.datetime.fromtimestamp(ts).astimezone().strftime(time_fmt),
                })
            return XTideGraph(self.location, unit, svgs, json.dumps(payload, separators=(',', ':')), events_display,
                              html.escape(self.get_credit()))
        except Exception as e:
            log.error('XTideGraphBuilder.build: %s (%s)' % (e, type(e)))
            weeutil.logger.log_traceback(log.error, "    ****  ")
            return None

    def run_tide(self, mode: str, begin: float, end: float, step: str) -> Optional[str]:
        try:
            completed = subprocess.run([self.prog, '-z', '-u', self.units, '-l', self.location, '-b', tide_utc_arg(begin), '-e', tide_utc_arg(end), '-fc', '-m', mode, '-s', step], capture_output=True, encoding='utf-8', timeout=10)
        except FileNotFoundError:
            log.error('%s not found' % self.prog)
            return None
        except subprocess.TimeoutExpired:
            log.error("tide for location %s timed out." % self.location)
            return None
        if completed.returncode != 0:
            log.error("Call to tide failed: loc='%s' rc=%d %s" % (self.location, completed.returncode, XTidePoller.extract_tide_error(completed.stderr)))
            return None
        return completed.stdout

    def get_credit(self) -> str:
        """Where the station's harmonic data come from, per tide's about
        mode: its Credit line, else its Source line, else ''.  Flater's
        free file carries Credit ("NOAA data processed by David Flater for
        XTide"); openwatersio's files carry only Source ("TICON-4"), and
        TICON-4's license asks for attribution.  A failure here costs the
        page its credit line, never the graph."""
        try:
            completed = subprocess.run([self.prog, '-l', self.location, '-m', 'a'],
                                       capture_output=True, encoding='utf-8', timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            # OSError, not just FileNotFoundError: a permission error or any
            # other failure to start tide must cost only the credit line.
            return ''
        if completed.returncode != 0:
            return ''
        # "Credit              NOAA data processed by David Flater for XTide"
        # "                    https://flaterco.com/xtide/"   (continuation: skipped)
        fields: Dict[str, str] = {}
        for line in completed.stdout.splitlines():
            m = re.match(r'(\S.*?)\s{2,}(\S.*)$', line)
            if m and m.group(1) not in fields:
                fields[m.group(1)] = m.group(2).strip()
        return fields.get('Credit') or fields.get('Source') or ''

    def get_samples(self, begin: float, end: float, step_secs: int) -> Optional[Tuple[int, int, List[float]]]:
        """Continuous levels from raw mode: (first timestamp, step, values)."""
        out = self.run_tide('r', begin, end, '%02d:%02d' % (step_secs // 3600, step_secs % 3600 // 60))
        if out is None:
            return None
        times: List[int] = []
        values: List[float] = []
        for line in out.splitlines():
            # "Palo Alto Yacht Harbor, ...",1783666800,5.286100
            cols = next(csv.reader([line]))
            if len(cols) != 3 or not cols[1].strip().isdigit():
                continue
            times.append(to_int(cols[1]))
            values.append(to_float(cols[2]))
        if len(values) < 2:
            log.error("tide raw mode returned no samples: loc='%s'" % self.location)
            return None
        return times[0], times[1] - times[0], values

    def get_events(self, begin: float, end: float) -> Optional[Tuple[List[Tuple[int, float, int]], List[Tuple[int, str]], str]]:
        """Tide extremes and sun events from plain mode: (tides, suns, unit).
        tides are (timestamp, level, EventType value); suns are (timestamp, 'Sunrise'|'Sunset')."""
        out = self.run_tide('p', begin, end, '01:00')
        if out is None:
            return None
        tides: List[Tuple[int, float, int]] = []
        suns: List[Tuple[int, str]] = []
        unit = ''
        for line in out.splitlines():
            cols = next(csv.reader([line]))
            if len(cols) != 5:
                continue
            try:
                ts = tide_event_ts(cols[1], cols[2])
            except ValueError:
                continue
            kind = cols[4]
            if kind in ('High Tide', 'Low Tide'):
                parts = cols[3].split(' ')
                unit = parts[1]
                tides.append((ts, to_float(parts[0]), XTidePoller.encode_event_type(kind).value))
            elif kind in ('Sunrise', 'Sunset'):
                suns.append((ts, kind))
        if not tides:
            log.error("tide plain mode returned no tide events: loc='%s'" % self.location)
            return None
        return tides, suns, unit

    @staticmethod
    def night_intervals(suns: List[Tuple[int, str]], begin: float, end: float) -> List[Tuple[float, float]]:
        """Sunset-to-sunrise intervals clipped to [begin, end]."""
        nights: List[Tuple[float, float]] = []
        sunset: Optional[int] = None
        for ts, kind in suns:
            if kind == 'Sunset':
                sunset = ts
            elif kind == 'Sunrise' and sunset is not None:
                if ts > begin and sunset < end:
                    nights.append((max(float(sunset), begin), min(float(ts), end)))
                sunset = None
        if sunset is not None and sunset < end:
            nights.append((max(float(sunset), begin), end))
        return nights

    @staticmethod
    def choose_tick(value_range: float) -> float:
        for step in (0.5, 1.0, 2.0, 5.0, 10.0):
            if value_range / step <= 8:
                return step
        return 20.0

    def build_view_svg(self, name: str, t0: int, t1: int, step: int, values: List[float],
                       tides: List[Tuple[int, float, int]], nights: List[Tuple[float, float]],
                       unit: str) -> Tuple[str, float, float]:
        pw = self.W - self.ML - self.MR
        ph = self.H - self.MT - self.MB
        levels = values + [ev[1] for ev in tides]
        tick = self.choose_tick(max(levels) - min(levels))
        vlo = math.floor(min(levels) / tick) * tick
        vhi = math.ceil(max(levels) / tick) * tick
        # Keep the curve clear of the frame: pad when an extreme lands on or
        # near a gridline (not just exactly on it).
        if vhi - max(levels) < 0.05 * tick:
            vhi += tick
        if min(levels) - vlo < 0.05 * tick:
            vlo -= tick

        def x(t: float) -> float:
            return self.ML + (t - t0) * pw / (t1 - t0)

        def y(v: float) -> float:
            return self.MT + (vhi - v) * ph / (vhi - vlo)

        s: List[str] = []
        s.append('<svg class="xg" data-view="%s" viewBox="0 0 %d %d" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">' % (name, self.W, self.H))
        # Night shading (sunset to sunrise)
        for n0, n1 in nights:
            s.append('<rect class="xg-night" x="%.1f" y="%d" width="%.1f" height="%d"/>' % (x(n0), self.MT, x(n1) - x(n0), ph))
        # Horizontal grid and level labels
        v = vlo
        while v <= vhi + tick / 2:
            s.append('<line class="xg-grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>' % (self.ML, y(v), self.W - self.MR, y(v)))
            s.append('<text class="xg-lab xg-ylab" x="%d" y="%.1f">%g</text>' % (self.ML - 8, y(v) + 4, v))
            v += tick
        # Vertical grid and time labels
        for tick_t, label in self.time_ticks(name, t0, t1):
            s.append('<line class="xg-grid" x1="%.1f" y1="%d" x2="%.1f" y2="%d"/>' % (x(tick_t), self.MT, x(tick_t), self.MT + ph))
            lx = min(max(x(tick_t), self.ML + 20), self.W - 24)
            s.append('<text class="xg-lab xg-xlab" x="%.1f" y="%d">%s</text>' % (lx, self.MT + ph + 18, label))
        # The tide curve
        points = ' '.join('%.1f,%.1f' % (x(t0 + i * step), y(v)) for i, v in enumerate(values))
        s.append('<polyline class="xg-curve" points="%s"/>' % points)
        # Event markers (labels on the day view only; elsewhere the tooltip serves)
        radius = {'day': 4.5, 'week': 3.5, 'month': 2.5}[name]
        # The leading zero is stripped only from 12-hour times ("9:16 AM");
        # a 24-hour "09:16" keeps it, including via a translated format.
        # This net works here because the time starts the string.  The event
        # ROW cannot use it -- its hour sits mid-string, after the date -- so
        # that format asks for %-I directly, and TestI18n's oracle is what
        # keeps a translator from putting a padded %I into a dated format.
        marker_fmt = self._raw('%I:%M %p') if self.hour12 else self._raw('%H:%M')
        for ts, level, event_type in tides:
            high = event_type == EventType.HIGH_TIDE.value
            px, py = x(ts), y(level)
            s.append('<circle class="%s" cx="%.1f" cy="%.1f" r="%s"/>' % ('xg-hi' if high else 'xg-lo', px, py, radius))
            if name == 'day':
                time_lbl = datetime.datetime.fromtimestamp(ts).astimezone().strftime(marker_fmt)
                if '%I' in marker_fmt:
                    time_lbl = time_lbl.lstrip('0')
                label = '%.2f %s · %s' % (level, unit, time_lbl)
                lx = min(max(px, self.ML + 60), self.W - self.MR - 60)
                # Above a high and below a low -- unless there is no room
                # there, in which case the label goes on the other side.
                # Clamping it inside the plot instead drew it over its own
                # marker whenever an extreme sat near the frame.
                if high:
                    ly = py - 12 if py - 12 >= self.MT + 12 else py + 20
                else:
                    ly = py + 20 if py + 20 <= self.MT + ph - 6 else py - 12
                # paint-order as an attribute, not in the stylesheet: the Nu
                # checker's CSS validator does not know the property and
                # fails xtide.css on it, while the SVG attribute validates.
                # It changes nothing until a skin gives the label a stroke
                # (the sample skin's halo).
                s.append('<text class="xg-lab xg-evlab" x="%.1f" y="%.1f" paint-order="stroke">%s</text>' % (lx, ly, label))
        # Unit reminder, frame, and the javascript-positioned "now" marker
        s.append('<text class="xg-lab xg-unitlab" x="%d" y="%d">%s</text>' % (self.ML + 8, self.MT + 16, self._t('Tide ({unit})', unit=unit)))
        s.append('<rect class="xg-frame" x="%d" y="%d" width="%d" height="%d"/>' % (self.ML, self.MT, pw, ph))
        s.append('<line class="xg-nowline" x1="-10" y1="%d" x2="-10" y2="%d"/>' % (self.MT, self.MT + ph))
        s.append('</svg>')
        return ''.join(s), vlo, vhi

    def time_ticks(self, name: str, t0: int, t1: int) -> List[Tuple[float, str]]:
        ticks: List[Tuple[float, str]] = []
        if name == 'day':
            hour_fmt = self._raw('%I %p') if self.hour12 else self._raw('%H')
            t: float = t0
            while t <= t1:
                dt = datetime.datetime.fromtimestamp(t).astimezone()
                label = dt.strftime(hour_fmt)
                if '%I' in hour_fmt:
                    label = label.lstrip('0')
                if dt.hour == 0:
                    label = dt.strftime('%a')
                ticks.append((t, label))
                t += 6 * 3600
        elif name == 'week':
            fmt = self._raw('%a %-d')
            for k in range(8):
                t = t0 + k * 86400
                ticks.append((t, datetime.datetime.fromtimestamp(t).astimezone().strftime(fmt)))
        else:
            fmt = self._raw('%b %-d')
            for k in range(0, 31, 5):
                t = t0 + k * 86400
                ticks.append((t, datetime.datetime.fromtimestamp(t).astimezone().strftime(fmt)))
        return ticks


class XTideVariables(SearchList):
    def __init__(self, generator):
        SearchList.__init__(self, generator)

        self.formatter = generator.formatter
        self.converter = generator.converter

        xtide_dict = generator.config_dict.get('XTide', {})
        self.binding = xtide_dict.get('data_binding', 'xtide_binding')

        self.time_group = weewx.units.obs_group_dict['dateTime']
        self.altitude_group = weewx.units.obs_group_dict['altitude']
        self.level_unit_format_dict= {'foot': '%0.2f', 'meter': '%0.2f'}
        # The unit names in the report's own language when its lang file
        # translates them (the sample skin's does), English otherwise.
        texts = generator.skin_dict.get('Texts', {})
        def label(key: str) -> str:
            val = texts.get(key, key)
            return ' ' + (val if isinstance(val, str) else key)
        self.level_unit_label_dict = {'foot': label('feet'), 'meter': label('meters')}

        # Keyed by the caller's presentation arguments: a page may embed the
        # graph twice with different typography, and running tide is not free.
        self._graphs: Dict[Any, Optional[XTideGraph]] = {}

    def get_extension_list(self, timespan, db_lookup) -> List[Dict[str, 'XTideVariables']]:
        return [{'xtide': self}]

    def graph(self, clock: Any = None, unit_label: Optional[str] = None) -> Optional[XTideGraph]:
        """The sample skin's tide graph, built once per distinct set of
        arguments per report generation.  Returns None (and the skin shows a
        hint) if tide could not be run.

        clock and unit_label are the CALLER's presentation choices, for a skin
        that embeds this graph in a page with typography of its own.  Pass
        clock=12 or clock=24 to state the page's clock instead of following
        the weewxd process locale; it drives the event rows, the graph's own
        labels and the tooltip alike.  Pass unit_label to replace the
        spelled-out 'feet'/'meters' in each row's level_str -- give it
        $unit.label.altitude and the label comes from the report's own
        formatter, so the page states the decision once, where WeeWX already
        keeps it.  Omit both and nothing changes."""
        key = (repr(clock), repr(unit_label))
        if key not in self._graphs:
            self._graphs[key] = None
            xtide_dict = self.generator.config_dict.get('XTide', {})
            location = config_location(xtide_dict)
            prog = xtide_dict.get('prog', '/usr/bin/tide')
            if location is None:
                log.error('graph: location must be specified.')
            else:
                # The report's [Texts], with the lang file already merged
                # over skin.conf by WeeWX; the builder translates the
                # strings it composes server-side.
                texts = self.generator.skin_dict.get('Texts', {})
                self._graphs[key] = XTideGraphBuilder(
                    prog, location, texts, tide_units(self.generator.converter),
                    clock, unit_label).build()
        return self._graphs[key]

    def events(self, max_events: Optional[int] = None) -> List[Dict[str, Any]]:
        """Returns tidal events."""

        rows = self.getEventRows(max_events)
        for row in rows:
            time_units = weewx.units.std_groups[row['usUnits']][self.time_group]
            # The report's converter and formatter: without them a ValueHelper
            # converts nothing (a level showed in the harmonics' units, never
            # the report's) and formats times with WeeWX's built-in default.
            row['dateTime'] = weewx.units.ValueHelper((row['dateTime'], time_units, self.time_group),
                formatter=self.formatter, converter=self.converter)
            row['location']  = row['location']
            row['eventType']  = 'High Tide' if row['eventType'] == EventType.HIGH_TIDE else 'Low Tide'
            altitude_units = weewx.units.std_groups[row['usUnits']][self.altitude_group]
            row['level'] = weewx.units.ValueHelper((row['level'], altitude_units, self.altitude_group),
                formatter=weewx.units.Formatter(unit_format_dict=self.level_unit_format_dict, unit_label_dict=self.level_unit_label_dict),
                converter=self.converter)
        return rows

    def getEventRows(self,  max_events: Optional[int] = None) -> List[Dict[str, Any]]:
        """get the latest tidal events"""
        try:
            dict = weewx.manager.get_manager_dict(self.generator.config_dict['DataBindings'],
                                                  self.generator.config_dict['Databases'],self.binding)
            with weewx.manager.open_manager(dict) as dbm:
                return XTideVariables.fetch_records(dbm, max_events)
        except Exception as e:
            log.error('getEventRows: %s (%s)' % (e, type(e)))
            weeutil.logger.log_traceback(log.error, "    ****  ")
            return []

    @staticmethod
    def fetch_records(dbm: weewx.manager.Manager, max_events: Optional[int] = None) -> List[Dict[str, Any]]:
        for i in range(3):
            try:
                return XTideVariables.fetch_records_internal(dbm, max_events)
            except Exception as e:
                # Main-thread reachable: saveEventsToDB -> select_events lands here.
                reraise_if_terminate(e)
                # Datbase locked exception has been observed.  If first try, print info and sleep 1s.
                if i < 2:
                    log.info('fetch_records failed with %s (%s), retrying.' % (e, type(e)))
                    time.sleep(1)
                else:
                    log.error('Fetch records failed with %s (%s).' % (e, type(e)))
                    weeutil.logger.log_traceback(log.error, "    ****  ")
        return []

    @staticmethod
    def fetch_records_internal(dbm: weewx.manager.Manager, max_events: Optional[int] = None) -> List[Dict[str, Any]]:
        select = 'SELECT dateTime, usUnits, location, eventType, level FROM archive ORDER BY dateTime'
        records = []
        event_count = 0
        for row in dbm.genSql(select):
                event_count += 1
                record = {}

                record['dateTime'] = row[0]
                record['usUnits'] = row[1]
                record['location'] = row[2]
                record['eventType'] = XTidePoller.event_type_from_int(row[3])
                record['level'] = row[4]

                records.append(record)
        return records

if __name__ == '__main__':
    usage = """%prog [options] [--help]"""

    import weeutil.logger

    def main():
        import optparse

        parser = optparse.OptionParser(usage=usage)
        parser.add_option('--test-service', dest='testserv', action='store_true',
                          help='Test the XTide service.  Requires --location.  Optional --prog.')
        parser.add_option('--test-tide-execution', dest='testexec', action='store_true',
                          help='Test fetching tidal events.  Requires --location.  Optional --prog.  Optional --days')
        parser.add_option('--location', type='str', dest='location',
                          help='The location for which tidal events are bing requested.')
        parser.add_option('--days', type='int', dest='days',
                          help='The number of days to fetch.')
        parser.add_option('--prog', type='str', dest='prog',
                          help='The location for which tide program (if not /usr/bin/tide).')
        parser.add_option('--view-events', dest='view', action='store_true',
                          help='View tidal events.  Must specify --xtide-database.')
        parser.add_option('--xtide-database', dest='db',
                          help='Location of xtide.sdb file (only works with sqlite3).')
        (options, args) = parser.parse_args()

        weeutil.logger.setup('xtide', {})

        if options.testserv:
            if not options.location:
                parser.error('--test-service requires --location')
            test_service(options.location, options.prog if options.prog else '/usr/bin/tide')

        if options.testexec:
            if not options.location:
                parser.error('--text-tide-execution requires --location argument')
            cfg = Configuration(
                lock      = threading.Lock(),
                location  = options.location,
                prog      = options.prog if options.prog else '/usr/bin/tide',
                days      = options.days if options.days else 7,
                events    = [],
                )
            if not os.path.isfile(cfg.prog):
                print('%s does not exist!' % cfg.prog)
                sys.exit(1)
            if XTidePoller.populate_tidal_events(cfg):
                for event in cfg.events:
                    print('dateTime: %s, type: %s, level: %f %s' % (timestamp_to_string(event.dateTime), event.eventType, event.level, 'ft' if event.usUnits == weewx.US else 'm'))
            else:
                print('Call to XTidePoller.populate_tidal_events failed.')

        if options.view:
            if not options.db:
                parser.error('--view-events requires --xtide-database argument')

            view_sqlite_database(options.db)

    def test_service(location: str, prog: str) -> None:
        from weewx.engine import StdEngine
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile() as temp_file:
            config = configobj.ConfigObj({
                'Station': {
                    'station_type': 'Simulator',
                    'altitude' : [0, 'foot'],
                    'latitude' : 37.431495,
                    'longitude': -122.110937},
                'Simulator': {
                    'driver': 'weewx.drivers.simulator',
                    'mode': 'simulator'},
                'StdArchive': {
                    'archive_interval': 300},
                'XTide': {
                    'binding': 'xtide_binding',
                    'location': location,
                    'prog': prog},
                'DataBindings': {
                    'xtide_binding': {
                        'database': 'xtide_sqlite',
                        'manager': 'weewx.manager.Manager',
                        'table_name': 'archive',
                        'schema': 'user.xtide.schema'}},
                'Databases': {
                    'xtide_sqlite': {
                        'database_name': temp_file.name,
                        'database_type': 'SQLite'}},
                'Engine': {
                    'Services': {
                        'data_services': 'user.xtide.XTide'}},
                'DatabaseTypes': {
                    'SQLite': {
                        'driver': 'weedb.sqlite'}}})
            engine = StdEngine(config)
            xtide = XTide(engine, config)

            rc = XTidePoller.populate_tidal_events(xtide.cfg)
            if rc:
                xtide.saveEventsToDB()

            for record in xtide.select_events():
                pretty_print_record(record)
                print('------------------------')

    def pretty_print_record(record) -> None:
        print('dateTime : %s' % timestamp_to_string(record['dateTime']))
        print('usUnits  : %s' % record['usUnits'])
        print('location : %s' % record['location'])
        print('eventType: %s' % record['eventType'])
        print('level    : %f' % record['level'])

    def view_sqlite_database(dbfile: str) -> None:
        try:
            import sqlite3
        except:
            print('Could not import sqlite3.')
            return
        conn = sqlite3.connect(dbfile)
        print_sqlite_records(conn, dbfile)

    def print_sqlite_records(conn, dbfile: str) -> None:
        select = "SELECT dateTime, usUnits, location, eventType, level FROM archive ORDER BY dateTime"

        for row in conn.execute(select):
            record = {}
            record['dateTime'] = row[0]
            record['usUnits'] = row[1]
            record['location'] = row[2]
            record['eventType'] = row[3]
            record['level'] = row[4]
            pretty_print_record(record)
            print('------------------------')


    main()
