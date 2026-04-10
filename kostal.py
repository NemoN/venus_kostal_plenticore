#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# vi: set autoindent noexpandtab tabstop=4 shiftwidth=4
import os
import sys

# Add bundled libraries to path (survives Venus OS updates)
_lib_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib')
if os.path.isdir(_lib_dir):
    sys.path.insert(0, _lib_dir)

import re
from configparser import ConfigParser

from plenticoreDataService import get_data, reset_energy_state
from loggingConfig import logger
from plenticoreSessionService import get_session_key

import requests
import json

from dbus_inverter import DbusInverter

from dbus.mainloop.glib import DBusGMainLoop

from gi.repository import GLib

import dbus
import dbus.service
import signal
import threading
import time

MAX_RECONNECT_DELAY = 60  # seconds

# Kostal Inverter:State -> Venus OS StatusCode mapping
# Venus OS codes: 0-6=Startup, 7=Running, 8=Standby, 9=Boot loading, 10=Error
KOSTAL_STATE_TO_STATUS = {
    0: 8,   # Off -> Standby
    1: 0,   # Init -> Startup
    2: 1,   # IsoMeas -> Startup
    3: 2,   # GridCheck -> Startup
    4: 3,   # StartUp -> Startup
    5: 0,   # - -> Startup
    6: 7,   # FeedIn -> Running
    7: 7,   # Throttled -> Running
    8: 8,   # ExtSwitchOff -> Standby
    9: 9,   # Update -> Boot loading
    10: 8,  # Standby -> Standby
    11: 4,  # GridSync -> Startup
    12: 5,  # GridPreCheck -> Startup
    13: 8,  # GridSwitchOff -> Standby
    14: 10, # Overheating -> Error
    15: 8,  # Shutdown -> Standby
    16: 10, # ImproperDcVoltage -> Error
    17: 10, # ESB -> Error
    18: 10, # Unknown -> Error
}


class DevState:
    WaitForDevice = 0
    Connect = 1
    Connected = 2


class DevStatistics:
    def __init__(self):
        self.connection_ok = 0
        self.connection_ko = 0
        self.parse_error = 0
        self.last_connection_errors = 0  # reset every ok read
        self.last_time = 0
        self.reconnect = 0


class Kostal:
    def __init__(self, name, ip, instance, password, interval, position):
        self.inverter_name = name
        self.ip = ip
        self.instance = instance
        self.password = password
        self.interval = interval
        self.position = position
        self.stats = DevStatistics()
        self.version = 1
        self.max_retries = 10
        self.session_id = 'XXX'
        self.sw_version = ''
        self.inv_settings = {'serial': '', 'product_name': '', 'max_power': None, 'string_cnt': 3}
        self.dev_state = DevState.WaitForDevice
        self.dbus_inverter = None
        self.reconnect_delay = 0  # for exponential backoff


global inverter

base_path = '/api/v1'


def push_statistics():
    global inverter
    inverter.dbus_inverter.set('/stats/connection_ok', inverter.stats.connection_ok)
    inverter.dbus_inverter.set('/stats/connection_error', inverter.stats.connection_ko)
    inverter.dbus_inverter.set('/stats/last_connection_errors', inverter.stats.last_connection_errors)
    inverter.dbus_inverter.set('/stats/parse_error', inverter.stats.parse_error)
    inverter.dbus_inverter.set('/stats/reconnect', inverter.stats.reconnect)


def parse_config():
    global inverter
    parser = ConfigParser()
    cfgname = 'kostal.ini'
    if len(sys.argv) > 1:
        cfgname = str(sys.argv[1])
    logger.info('Parsing config: ' + cfgname)
    parser.read(cfgname)

    if len(parser.sections()) == 0:
        logger.warn("config seems to be empty...")
        exit(1)

    def get_password(section):
        if parser.has_option(section, 'password'):
            return parser.get(section, 'password')
        else:
            logger.warn('config section ' + section + ' is missing the password.')
            exit(1)

    def get_ip(section):
        if parser.has_option(section, 'ip'):
            ip = parser.get(section, 'ip')
            match = re.match(r"http:\/\/[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", ip)
            if match is None or match.span() != (0, len(ip)):
                logger.warn("Error: ip should be of format: 'http://123.123.123.123', instead got '" + ip + "'")
                exit(1)
            return ip + base_path
        else:
            logger.warn('config section ' + section + ' is missing the ip..')
            exit(1)

    def get_interval(section):
        if parser.has_option(section, 'interval'):
            return int(parser.get(section, 'interval'))
        else:
            logger.warn('config section ' + section + ' is missing the interval..')
            exit(1)

    def get_instance(section):
        if parser.has_option(section, 'instance'):
            return int(parser.get(section, 'instance'))
        else:
            logger.warn('config section ' + section + ' is missing the instance, using default 50...')
            return 50

    def get_position(section):
        if parser.has_option(section, 'position'):
            return int(parser.get(section, 'position'))
        else:
            logger.warn('config section ' + section + ' is missing the position, using default 0...')
            return 0

    section = parser.sections()[0]

    inverter = Kostal(section, get_ip(section), get_instance(section), get_password(section), get_interval(section),
                      get_position(section))

    logger.info('Found config: ' + section)

    logger.info(inverter.inverter_name + ' at ' + inverter.ip)

    # Optional: configurable log level
    if parser.has_option(section, 'loglevel'):
        from loggingConfig import set_log_level
        set_log_level(parser.get(section, 'loglevel'))


def set_dbus_data(data):
    global inverter
    time_ms = int(round(time.time() * 1000))
    if inverter.stats.last_time == time_ms:
        inverter.dbus_inverter.inc('/stats/repeated_values')
        inverter.dbus_inverter.inc('/stats/last_repeated_values')
        logger.info('got repeated value')
    else:
        inverter.stats.last_time = time_ms
        inverter.dbus_inverter.set('/stats/last_repeated_values', 0)

        inverter.dbus_inverter.set('/Ac/Power', (data['PT']), 1)
        inverter.dbus_inverter.set('/Ac/Current', (data['IN0']), 1)
        inverter.dbus_inverter.set('/Ac/L1/Current', (data['IA']), 2)
        inverter.dbus_inverter.set('/Ac/L1/Voltage', (data['VA']), 1)
        inverter.dbus_inverter.set('/Ac/L1/Power', (data['PA']), 1)
        inverter.dbus_inverter.set('/Ac/L1/Energy/Forward', data['EA'], 3)
        inverter.dbus_inverter.set('/Ac/L2/Current', (data['IB']), 2)
        inverter.dbus_inverter.set('/Ac/L2/Voltage', (data['VB']), 1)
        inverter.dbus_inverter.set('/Ac/L2/Power', (data['PB']), 1)
        inverter.dbus_inverter.set('/Ac/L2/Energy/Forward', data['EB'], 3)
        inverter.dbus_inverter.set('/Ac/L3/Current', (data['IC']), 2)
        inverter.dbus_inverter.set('/Ac/L3/Voltage', (data['VC']), 1)
        inverter.dbus_inverter.set('/Ac/L3/Power', (data['PC']), 1)
        inverter.dbus_inverter.set('/Ac/L3/Energy/Forward', data['EC'], 3)

        inverter.dbus_inverter.set('/Ac/Energy/Forward', data['EFAT'], 3)

        inverter.dbus_inverter.set('/Ac/Voltage', data['VA'], 1)
        inverter.dbus_inverter.set('/Ac/Frequency', data['FREQ'], 2)

        if data.get('PV1_U') is not None:
            inverter.dbus_inverter.set('/Dc/0/Voltage', data['PV1_U'], 1)
            inverter.dbus_inverter.set('/Dc/0/Current', data['PV1_I'], 2)
            inverter.dbus_inverter.set('/Dc/0/Power', data['PV1_P'], 1)
            inverter.dbus_inverter.set('/Dc/1/Voltage', data['PV2_U'], 1)
            inverter.dbus_inverter.set('/Dc/1/Current', data['PV2_I'], 2)
            inverter.dbus_inverter.set('/Dc/1/Power', data['PV2_P'], 1)
            if inverter.inv_settings['string_cnt'] >= 3:
                inverter.dbus_inverter.set('/Dc/2/Voltage', data['PV3_U'], 1)
                inverter.dbus_inverter.set('/Dc/2/Current', data['PV3_I'], 2)
                inverter.dbus_inverter.set('/Dc/2/Power', data['PV3_P'], 1)

        if data.get('INV_STATE') is not None:
            inverter.dbus_inverter.set('/StatusCode', KOSTAL_STATE_TO_STATUS.get(data['INV_STATE'], 10))

        logger.debug("++++++++++")
        logger.debug("POWER Phase A: " + str(data['PA']) + "W")
        logger.debug("POWER Phase B: " + str(data['PB']) + "W")
        logger.debug("POWER Phase C: " + str(data['PC']) + "W")
        logger.debug("POWER Total: " + str(data['PT']) + "W")
        logger.debug("Energy Total: " + str(data['EFAT']) + "W")


def init_session():
    global inverter
    session_id, sw_version, api_version, inv_settings = get_session_key(inverter.password, inverter.ip)
    inverter.sw_version = sw_version
    inverter.inv_settings = inv_settings
    inverter.session_id = session_id
    inverter.dev_state = DevState.Connected
    if inverter.dbus_inverter:
        inverter.dbus_inverter.set('/Connected', 1)
    logger.info('Session initialized successfully')


def init_dbus():
    global inverter
    s = inverter.inv_settings
    inverter.dbus_inverter = DbusInverter(inverter.inverter_name, inverter.ip, inverter.instance, s['serial'],
                                          s['product_name'],
                                          inverter.sw_version, '0.1', inverter.position,
                                          max_power=s['max_power'], string_cnt=s['string_cnt'])
    return


def invalidate_dbus_data():
    global inverter
    inverter.dbus_inverter.set('/Connected', 0)
    inverter.dbus_inverter.set('/Ac/L1/Current', None)
    inverter.dbus_inverter.set('/Ac/L2/Current', None)
    inverter.dbus_inverter.set('/Ac/L3/Current', None)
    inverter.dbus_inverter.set('/Ac/L1/Power', None)
    inverter.dbus_inverter.set('/Ac/L2/Power', None)
    inverter.dbus_inverter.set('/Ac/L3/Power', None)
    inverter.dbus_inverter.set('/Ac/L1/Voltage', None)
    inverter.dbus_inverter.set('/Ac/L2/Voltage', None)
    inverter.dbus_inverter.set('/Ac/L3/Voltage', None)
    inverter.dbus_inverter.set('/Ac/Power', None)
    inverter.dbus_inverter.set('/Ac/Current', None)
    inverter.dbus_inverter.set('/Ac/Voltage', None)
    inverter.dbus_inverter.set('/Ac/Frequency', None)
    inverter.dbus_inverter.set('/Dc/0/Voltage', None)
    inverter.dbus_inverter.set('/Dc/0/Current', None)
    inverter.dbus_inverter.set('/Dc/0/Power', None)
    inverter.dbus_inverter.set('/Dc/1/Voltage', None)
    inverter.dbus_inverter.set('/Dc/1/Current', None)
    inverter.dbus_inverter.set('/Dc/1/Power', None)
    if inverter.inv_settings['string_cnt'] >= 3:
        inverter.dbus_inverter.set('/Dc/2/Voltage', None)
        inverter.dbus_inverter.set('/Dc/2/Current', None)
        inverter.dbus_inverter.set('/Dc/2/Power', None)
    inverter.dbus_inverter.set('/StatusCode', None)


def reconnect():
    global inverter

    # Exponential backoff: wait before reconnecting
    if inverter.reconnect_delay > 0:
        logger.info('Waiting {}s before reconnect attempt'.format(inverter.reconnect_delay))
        time.sleep(inverter.reconnect_delay)

    try:
        logger.info('Trying to reconnect to ' + inverter.ip)
        init_session()
        reset_energy_state()
        inverter.stats.last_connection_errors = 0
        inverter.reconnect_delay = 0
        logger.info('Reconnect successful')
        return True
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError) as err:
        logger.warning('Reconnect failed for ' + inverter.ip + ': ' + str(err))
        inverter.stats.connection_ko += 1
        inverter.stats.last_connection_errors += 1
        # Increase backoff: 1, 2, 4, 8, 16, 32, 60, 60, ...
        if inverter.reconnect_delay == 0:
            inverter.reconnect_delay = 1
        else:
            inverter.reconnect_delay = min(inverter.reconnect_delay * 2, MAX_RECONNECT_DELAY)
        return False

def read_data():
    global inverter
    try:
        logger.debug('reading data from ' +
              inverter.inverter_name + ' inverter at ' + inverter.ip + ' using sessionid ' + inverter.session_id)
        data = get_data(inverter.ip, inverter.session_id)
        set_dbus_data(data)
        inverter.stats.connection_ok += 1
        inverter.stats.last_connection_errors = 0
        inverter.dbus_inverter.set('/Connected', 1)
        logger.debug('done.')
        return
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError) as err:
        logger.warning('Error reading from ' + inverter.ip + ': ' + str(err))
        inverter.stats.connection_ko += 1
        inverter.stats.last_connection_errors += 1
        return 1


def cyclic_update(run_event):
    global inverter

    while run_event.is_set():
        logger.debug("Thread: doing")

        push_statistics()

        if inverter.stats.last_connection_errors > inverter.max_retries:
            logger.warn('Lost connection to kostal, reset')
            inverter.dev_state = DevState.Connect
            inverter.stats.last_connection_errors = 0
            inverter.stats.reconnect += 1
            invalidate_dbus_data()

        elif inverter.dev_state == DevState.Connected:
            read_data()
        elif inverter.dev_state == DevState.Connect:
            reconnect()
        else:
            logger.error('invalid state...')

        time.sleep(inverter.interval)
    return


def shutdown(signum=None, frame=None):
    """Graceful shutdown handler for SIGTERM/SIGINT."""
    sig_name = signal.Signals(signum).name if signum else 'unknown'
    logger.info('Received ' + sig_name + ', shutting down...')
    run_event.clear()
    mainloop.quit()


DBusGMainLoop(set_as_default=True)
parse_config()
try:
    init_session()
except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError) as err:
    logger.warning('Initial session setup failed for ' + inverter.ip + ': ' + str(err))
    inverter.dev_state = DevState.Connect
init_dbus()
if inverter.dev_state != DevState.Connected:
    invalidate_dbus_data()

run_event = threading.Event()
run_event.set()

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

try:
    update_thread = threading.Thread(target=cyclic_update, args=(run_event,))
    update_thread.daemon = True
    update_thread.start()

    mainloop = GLib.MainLoop()
    mainloop.run()

except (KeyboardInterrupt, SystemExit):
    pass
finally:
    run_event.clear()
    update_thread.join(timeout=10)
    logger.info('Shutdown complete')
