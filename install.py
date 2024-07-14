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

from setup import ExtensionInstaller

def loader():
    return XTideInstaller()

class XTideInstaller(ExtensionInstaller):
    def __init__(self):
        super(XTideInstaller, self).__init__(
            version="1.0.2",
            name='xtide',
            description='Fetch Tide Forecasts.',
            author="John A Kline",
            author_email="john@johnkline.com",
            data_services='user.xtide.XTide',
            config={
                'XTide': {
                    'data_binding': 'xtide_binding',
                    'location'    : 'Palo Alto Yacht Harbor, San Francisco Bay, California',
                    'days'        : 7,
                    'prog'        : '/usr/bin/tide',
                },
                'DataBindings': {
                    'xtide_binding': {
                        'manager'   : 'weewx.manager.Manager',
                        'schema'    : 'user.xtide.schema',
                        'table_name': 'archive',
                        'database'  : 'xtide_sqlite'
                    }
                },
                'Databases': {
                    'xtide_sqlite': {
                        'database_name': 'xtide.sdb',
                        'driver'       : 'weedb.sqlite'
                    }
                },
                'StdReport': {
                    'XTideReport': {
                        'HTML_ROOT':'xtide',
                        'enable'   : 'true',
                        'skin'     :'xtide',
                    },
                },
            },
            files=[
                ('bin/user', ['bin/user/xtide.py']),
                ('skins/xtide', [
                    'skins/xtide/index.html.tmpl',
                    'skins/xtide/skin.conf',
                    'skins/xtide/xtide_icons/high-tide.png',
                    'skins/xtide/xtide_icons/low-tide.png',
                ]),
            ]
        )
