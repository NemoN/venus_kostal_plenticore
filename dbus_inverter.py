#!/usr/bin/env python3

try:
    import gobject
    from gobject import idle_add
except:
    from gi.repository import GObject as gobject
    from gi.repository.GObject import idle_add
import json
import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))

from vedbus import VeDbusService
from loggingConfig import logger

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'settings.json')


def _load_settings():
    try:
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def _save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f)
    except IOError as err:
        logger.warning('Could not save settings: ' + str(err))


class DbusInverter:
    dbusservice = []

    def __init__(self, name, connection, device_instance, serial, product_name, firmware_version, process_version,
                 position, max_power=None, string_cnt=3):

        logger.info("Starting up, connecting as pvinverter '" + name + "' with vrm instance '" + str(
            device_instance) + "' to dbus and position '" + str(position) + "'")

        # Put ourselves on to the dbus
        dbus_name = 'com.victronenergy.pvinverter.' + name
        logger.info('dbus_name: ' + dbus_name)
        self.dbusservice = VeDbusService(dbus_name, register=False)

        # Add objects required by ve-api
        self.dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self.dbusservice.add_path('/Mgmt/ProcessVersion', process_version)
        self.dbusservice.add_path('/Mgmt/Connection', connection)  # todo
        self.dbusservice.add_path('/DeviceInstance', device_instance)
        self.dbusservice.add_path('/ProductId', 0xFFFF)  # 0xB012 ?
        self.dbusservice.add_path('/ProductName', product_name)
        self.dbusservice.add_path('/FirmwareVersion', firmware_version)
        self.dbusservice.add_path('/Serial', serial)
        self.dbusservice.add_path('/Connected', 1, writeable=True)
        self.dbusservice.add_path('/ErrorCode', '(0) No Error')
        self.dbusservice.add_path('/Position', position)

        # Load persisted CustomName or use product_name as default
        settings = _load_settings()
        custom_name = settings.get('custom_name_' + name, product_name)
        self._service_name = name
        self.dbusservice.add_path('/CustomName', custom_name, writeable=True,
                                  onchangecallback=self._on_custom_name_changed)

        _kwh = lambda p, v: (str(v) + 'KWh')
        _a = lambda p, v: (str(v) + 'A')
        _w = lambda p, v: (str(v) + 'W')
        _v = lambda p, v: (str(v) + 'V')
        _s = lambda p, v: (str(v) + 's')
        _hz = lambda p, v: (str(v) + 'Hz')
        _x = lambda p, v: (str(v))

        self.dbusservice.add_path('/Ac/Energy/Forward', None, gettextcallback=_kwh)
        self.dbusservice.add_path('/Ac/L1/Current', None, gettextcallback=_a)
        self.dbusservice.add_path('/Ac/L1/Energy/Forward', None, gettextcallback=_kwh)
        self.dbusservice.add_path('/Ac/L1/Power', None, gettextcallback=_w)
        self.dbusservice.add_path('/Ac/L1/Voltage', None, gettextcallback=_v)
        self.dbusservice.add_path('/Ac/L2/Current', None, gettextcallback=_a)
        self.dbusservice.add_path('/Ac/L2/Energy/Forward', None, gettextcallback=_kwh)
        self.dbusservice.add_path('/Ac/L2/Power', None, gettextcallback=_w)
        self.dbusservice.add_path('/Ac/L2/Voltage', None, gettextcallback=_v)
        self.dbusservice.add_path('/Ac/L3/Current', None, gettextcallback=_a)
        self.dbusservice.add_path('/Ac/L3/Energy/Forward', None, gettextcallback=_kwh)
        self.dbusservice.add_path('/Ac/L3/Power', None, gettextcallback=_w)
        self.dbusservice.add_path('/Ac/L3/Voltage', None, gettextcallback=_v)
        self.dbusservice.add_path('/Ac/Power', None, gettextcallback=_w)
        self.dbusservice.add_path('/Ac/Current', None, gettextcallback=_a)
        self.dbusservice.add_path('/Ac/Voltage', None, gettextcallback=_v)
        self.dbusservice.add_path('/Ac/Frequency', None, gettextcallback=_hz)
        if max_power is not None:
            self.dbusservice.add_path('/Ac/MaxPower', max_power, gettextcallback=_w)

        self.dbusservice.add_path('/Dc/0/Voltage', None, gettextcallback=_v)
        self.dbusservice.add_path('/Dc/0/Current', None, gettextcallback=_a)
        self.dbusservice.add_path('/Dc/0/Power', None, gettextcallback=_w)
        self.dbusservice.add_path('/Dc/1/Voltage', None, gettextcallback=_v)
        self.dbusservice.add_path('/Dc/1/Current', None, gettextcallback=_a)
        self.dbusservice.add_path('/Dc/1/Power', None, gettextcallback=_w)
        if string_cnt >= 3:
            self.dbusservice.add_path('/Dc/2/Voltage', None, gettextcallback=_v)
            self.dbusservice.add_path('/Dc/2/Current', None, gettextcallback=_a)
            self.dbusservice.add_path('/Dc/2/Power', None, gettextcallback=_w)

        self.dbusservice.add_path('/StatusCode', None, gettextcallback=_x)

        self.dbusservice.add_path('/stats/connection_ok', 0, gettextcallback=_x, writeable=True)
        self.dbusservice.add_path('/stats/connection_error', 0, gettextcallback=_x, writeable=True)
        self.dbusservice.add_path('/stats/parse_error', 0, gettextcallback=_x, writeable=True)
        self.dbusservice.add_path('/stats/repeated_values', 0, gettextcallback=_x, writeable=True)
        self.dbusservice.add_path('/stats/last_connection_errors', 0, gettextcallback=_x, writeable=True)
        self.dbusservice.add_path('/stats/last_repeated_values', 0, gettextcallback=_x, writeable=True)
        self.dbusservice.add_path('/stats/reconnect', 0, gettextcallback=_x)
        self.dbusservice.add_path('/Mgmt/intervall', 1, gettextcallback=_s, writeable=True)

        self.dbusservice.register()

    def _on_custom_name_changed(self, path, value):
        settings = _load_settings()
        settings['custom_name_' + self._service_name] = value
        _save_settings(settings)
        logger.info('CustomName changed to: ' + str(value))
        return True

    def invalidate(self):
        self.set('/Ac/L1/Power', [])
        self.set('/Ac/L2/Power', [])
        self.set('/Ac/L3/Power', [])
        self.set('/Ac/Power', [])

    def set(self, name, value, round_digits=0):
        if isinstance(value, float):
            self.dbusservice[name] = round(value, round_digits)
        else:
            self.dbusservice[name] = value

    def get(self, name):
        v = self.dbusservice[name]
        return v

    def inc(self, name):
        self.dbusservice[name] += 1
