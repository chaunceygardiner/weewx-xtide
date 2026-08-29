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

from io import StringIO

import configobj

from setup import ExtensionInstaller

# Written as weewx.conf text rather than a dict so that the stanza weectl
# merges into a fresh weewx.conf arrives with its comments: ConfigObj keeps
# them, a dict has nowhere to put them.  An option that only selects a
# default is written commented out, so that the extension's own fallback --
# and a better one in some later release -- goes on governing; weectl fills
# in absent keys only and never rewrites a value that is already there, so a
# value written live here would pin the station to it for ever.
#
# Live in [XTide]: data_binding, location and prog.  days is the one option
# written commented out.
#
# ORDER MATTERS: ConfigObj attaches a comment block to the NEXT key, and
# conditional_merge transfers a key's comments only when it CREATES that key.
# [XTide] is followed by top-level [DataBindings], which every real weewx.conf
# already has, so a comment block left last in [XTide] is not merely
# misplaced -- it is dropped entirely.  Hence prog last in [XTide].  For the
# same reason the prose for [DataBindings], [Databases] and [StdReport] hangs
# off the subsection the installer creates, never off the section header it
# does not.
#
# location MUST stay quoted: its value contains commas, which ConfigObj reads
# as a list separator, and the extension wants a string.
CONFIG = """
[XTide]
    # This section configures the weewx-xtide extension.  See the README.md
    # for details.
    #
    # An option shown commented out is one the extension supplies itself.
    # Leave it commented and the extension's own value governs, including a
    # better one a later release might bring.  Uncomment it to pin this
    # station to the value written here.

    # The data binding used for the tide database.  The install also seeds
    # the matching xtide_binding and xtide_sqlite entries under
    # [DataBindings] and [Databases]; there is no reason to change any of it.
    data_binding = xtide_binding

    # How many days of tidal events to keep in the database for
    # $xtide.events().  The sample report's graph is not affected: it runs
    # the tide program itself at report time and always covers 30 days.
    #days = 7

    # PLACEHOLDER -- replace with the tide station to report on.  XTide
    # matches the name by its own prefix rules; the stations it knows are
    # listed at https://flaterco.com/xtide/locations.html.  A station in a
    # timezone other than the server's is fine.
    location = "Palo Alto Yacht Harbor, San Francisco Bay, California"

    # PLACEHOLDER -- where XTide's tide program is.  This value is
    # historical: an XTide built from source as the README describes lands
    # in /usr/local/bin, so this almost certainly has to be edited.
    prog = /usr/bin/tide

[DataBindings]
    [[xtide_binding]]
        # weewx-xtide keeps its tidal events in a database of its own
        # (xtide.sdb), separate from the weather archive.
        manager = weewx.manager.Manager
        schema = user.xtide.schema
        table_name = archive
        database = xtide_sqlite

[Databases]
    [[xtide_sqlite]]
        # The database named by xtide_binding above.  It lands in the same
        # directory as the weather archive.
        database_name = xtide.sdb
        driver = weedb.sqlite

[StdReport]
    [[XTideReport]]
        # The "XTideReport" uses the "xtide" skin, which showcases the
        # extension: an interactive tide graph and the tidal events that go
        # with it.  Files are placed in a dedicated subdirectory.
        HTML_ROOT = xtide
        enable = true
        skin = xtide
"""

xtide_dict = configobj.ConfigObj(StringIO(CONFIG), encoding='utf-8')

def loader():
    return XTideInstaller()

class XTideInstaller(ExtensionInstaller):
    def __init__(self):
        super(XTideInstaller, self).__init__(
            version="3.0",
            name='xtide',
            description='Fetch Tide Forecasts.',
            author="John A Kline",
            author_email="john@johnkline.com",
            data_services='user.xtide.XTide',
            config=xtide_dict,
            files=[
                ('bin/user', ['bin/user/xtide.py']),
                ('skins/xtide', [
                    'skins/xtide/index.html.tmpl',
                    'skins/xtide/skin.conf',
                    'skins/xtide/xtide.css',
                    'skins/xtide/xtide.js',
                    'skins/xtide/xtide_icons/high-tide.png',
                    'skins/xtide/xtide_icons/low-tide.png',
                    'skins/xtide/lang/da.conf',
                    'skins/xtide/lang/de.conf',
                    'skins/xtide/lang/en.conf',
                    'skins/xtide/lang/es.conf',
                    'skins/xtide/lang/fr.conf',
                    'skins/xtide/lang/it.conf',
                    'skins/xtide/lang/nl.conf',
                    'skins/xtide/lang/no.conf',
                    'skins/xtide/lang/sv.conf',
                ]),
            ]
        )
