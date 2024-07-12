#!/usr/bin/python3
# Copyright 2024 by John A Kline <john@johnkline.com>
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
import datetime
import logging
import os
import subprocess
import sys
import threading
import time


from enum import Enum
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import weewx
import weewx.units
import weeutil.weeutil

from weeutil.weeutil import timestamp_to_string
from weeutil.weeutil import to_float
from weeutil.weeutil import to_int
from weewx.engine import StdService
from weewx.cheetahgenerator import SearchList

log = logging.getLogger(__name__)

WEEWX_XTIDE_VERSION = "1.0"

if sys.version_info[0] < 3:
    raise weewx.UnsupportedFeature(
        "weewx-xtide requires Python 3, found %s" % sys.version_info[0])

if weewx.__version__ < "4":
    raise weewx.UnsupportedFeature(
        "WeeWX 4 is required, found %s" % weewx.__version__)

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

        location    = self.xtide_config_dict.get('location', None)
        if location is None:
            log.error('location must be spcified.')
            return

        self.cfg = Configuration(
            lock        = threading.Lock(),
            location    = location,
            prog        = self.xtide_config_dict.get('prog', '/usr/bin/tide'),
            days = to_int(self.xtide_config_dict.get('days', 14)),
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
               log.error('delete_all_events: %s failed with %s (%s).' % (select, e, type(e)))
               weeutil.logger.log_traceback(log.error, "    ****  ")
               return
           # If there are events, delete them.
           if row[0] != 0:
               delete = 'DELETE FROM archive'
               dbmanager.getSql(delete)
        except Exception as e:
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
            log.info('poll_xtide: Sleeping for %f seconds.' % sleep_time)
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
                p = subprocess.Popen([cfg.prog, '-l', cfg.location, '-b', timestamp_to_string(begin), '-e', timestamp_to_string(end), '-f' 'c', '-m', 'p', '-s', '01:00'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                rc = p.returncode
                if rc is not None:
                    log.error("Call to tide failed: loc='%s' rc=%s" % (cfg.location, rc))
                    return False
                # /home/jkline/software/xtide-2.15.5/tide -l "Palo Alto Yacht Harbor" -b "2024-07-07 00:00" -e "2024-07-15 00:00" -f c -m p -s "01:00"
                # Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,1:12 AM PDT,8.50 ft,High Tide
                # Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,5:54 AM PDT,,Sunrise
                # Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,7:24 AM PDT,,Moonrise
                # Palo Alto Yacht Harbor| San Francisco Bay| California,2024-07-07,9:31 AM PDT,-0.64 ft,Low Tide
                out = []
                if p.stdout:
                    for bytes in p.stdout:
                        line = bytes.decode('utf-8')
                        if line.count(',') == 4:
                            out.append(line)
                        else:
                            log.info("ignoring line: %s" % line)
                if out:
                    log.info('tide returned %d lines.' % len(out))
                    cfg.events = []
                    for line in out:
                        line = line.replace('\n', '')
                        cols = line.split(',')
                        eventType  = XTidePoller.encode_event_type(cols[4])
                        if eventType == EventType.HIGH_TIDE or eventType == EventType.LOW_TIDE:
                            unit = cols[3].split(' ')[1]
                            cfg.events.append(Event(
                                dateTime  = to_int(datetime.datetime.strptime('%s %s' % (cols[1], cols[2]), '%Y-%m-%d %I:%M %p %Z').timestamp()), # 2024-07-07 8:32 PM PDT
                                usUnits   = weewx.US if unit == 'ft' else weewx.METRIC,
                                location  = cols[0].replace('|', ','),
                                eventType = eventType,
                                level     = to_float(cols[3].split(' ')[0]),
                            ))
                    log.info('Fetched %d events.'  % len(out))
                    return True
                else:
                    # There was no output
                    if p.stderr:
                        for bytes in p.stderr:
                            line = bytes.decode('utf-8')
                            line = line.replace('\n', '')
                            if line.startswith('XTide Fatal Error:'):
                                log.error(line)
                    return False
            except FileNotFoundError:
                log.error('%s not found' % cfg.prog)
                return False
            except subprocess.CalledProcessError as cpe:
                log.error("tide for location %s failed rc:%d %s" % (cfg.location, cpe.returncode, cpe.output))
                return False

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
        self.level_unit_label_dict = {'foot': ' feet', 'meter': ' meters'}

    def get_extension_list(self, timespan, db_lookup) -> List[Dict[str, 'XTideVariables']]:
        return [{'xtide': self}]

    def events(self, max_events: Optional[int] = None) -> List[Dict[str, Any]]:
        """Returns tidal events."""

        rows = self.getEventRows(max_events)
        for row in rows:
            time_units = weewx.units.std_groups[row['usUnits']][self.time_group]
            row['dateTime'] = weewx.units.ValueHelper((row['dateTime'], time_units, self.time_group))
            row['location']  = row['location']
            row['eventType']  = 'High Tide' if row['eventType'] == EventType.HIGH_TIDE else 'Low Tide'
            altitude_units = weewx.units.std_groups[row['usUnits']][self.altitude_group]
            row['level'] = weewx.units.ValueHelper((row['level'], altitude_units, self.altitude_group),
                formatter=weewx.units.Formatter(unit_format_dict=self.level_unit_format_dict, unit_label_dict=self.level_unit_label_dict))
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
                          help='Test the XTide service.  Requires --location.')
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
            test_service(options.location)

        if options.testexec:
            if not options.location:
                parser.error('--text-tide-execution requires --location argument')
            cfg = Configuration(
                lock      = threading.Lock(),
                location  = options.location,
                prog      = options.prog if options.prog else '/usr/bin/tide',
                days      = options.days if options.days else 14,
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

    def test_service(location: str) -> None:
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
                    'location': 'Palo Alto',
                    'prog': '/home/jkline/software/xtide-2.15.5/tide'},
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
