# Copyright 2014, 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import urwid
from tornado.ioloop import IOLoop
from cloudinstall.ui.palette import STYLES

import logging

log = logging.getLogger('eventloop')


def build_loop(ui, config, **kwargs):
    """ Returns event loop configured with color palette

    Arguments:
    ui: Interface component
    config: Applications configuration
    kwargs: Additional keyword items to pass to urwid.MainLoop

    Returns:
    urwid.MainLoop()
    """
    opts = {
        'screen': urwid.raw_display.Screen(),
        'unhandled_input': None,
        'handle_mouse': True
    }
    opts['screen'].set_terminal_properties(colors=256)
    opts['screen'].reset_default_terminal_palette()
    opts.update(**kwargs)
    evl = urwid.TornadoEventLoop(IOLoop())
    return urwid.MainLoop(
        ui, STYLES, event_loop=evl, **opts)


def exit_loop():
    """ Exits urwid.MainLoop
    """
    urwid.ExitMainLoop()


def redraw_screen(loop):
    """ Redraws screen

    Arguments:
    loop: urwid.MainLoop
    """
    loop.draw_screen()


def set_alarm_in(loop, interval, cb):
    """ Sets an alarm to be executed later

    Arguments:
    loop: urwid.MainLoop
    interval: Time in seconds
    cb: callback function
    """
    loop.set_alarm_in(interval, cb)


def remove_alarm(loop, handle):
    """ Removes an alarm

    Arguments:
    loop: urwid.MainLoop
    handle: previous alarm callers handle
    """
    loop.remove_alarm(handle)


def run_loop(loop):
    """ Starts event loop

    Arguments:
    loop: urwid.MainLoop
    """
    loop.run()
