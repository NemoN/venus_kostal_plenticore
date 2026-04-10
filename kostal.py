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

from plenticoreDataService import PlenticoreDataService
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
        self.data_service = PlenticoreDataService(name)


base_path = '/api/v1'


def push_statistics(inv):
    inv.dbus_inverter.set('/stats/connection_ok', inv.stats.connection_ok)
    inv.dbus_inverter.set('/stats/connection_error', inv.stats.connection_ko)
    inv.dbus_inverter.set('/stats/last_connection_errors', inv.stats.last_connection_errors)
    inv.dbus_inverter.set('/stats/parse_error', inv.stats.parse_error)
    inv.dbus_inverter.set('/stats/reconnect', inv.stats.reconnect)


def parse_config():
    parser = ConfigParser()
    cfgname = 'kostal.ini'
    if len(sys.argv) > 1:
        cfgname = str(sys.argv[1])
    logger.info('Parsing config: ' + cfgname)
    parser.read(cfgname)

    if len(parser.sections()) == 0:
        logger.warning("config seems to be empty...")
        sys.exit(1)

    def get_password(section):
        if parser.has_option(section, 'password'):
            return parser.get(section, 'password')
        else:
            logger.warning('config section ' + section + ' is missing the password.')
            sys.exit(1)

    def get_ip(section):
        if parser.has_option(section, 'ip'):
            ip = parser.get(section, 'ip')
            match = re.match(r"http:\/\/[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", ip)
            if match is None or match.span() != (0, len(ip)):
                logger.warning("Error: ip should be of format: 'http://123.123.123.123', instead got '" + ip + "'")
                sys.exit(1)
            return ip + base_path
        else:
            logger.warning('config section ' + section + ' is missing the ip..')
            sys.exit(1)

    def get_interval(section):
        if parser.has_option(section, 'interval'):
            return int(parser.get(section, 'interval'))
        else:
            logger.warning('config section ' + section + ' is missing the interval..')
            sys.exit(1)

    def get_instance(section):
        if parser.has_option(section, 'instance'):
            return int(parser.get(section, 'instance'))
        else:
            logger.warning('config section ' + section + ' is missing the instance, using default 50...')
            return 50

    def get_position(section):
        if parser.has_option(section, 'position'):
            return int(parser.get(section, 'position'))
        else:
            logger.warning('config section ' + section + ' is missing the position, using default 0...')
            return 0

    inverters = []
    log_level_set = False
    for section in parser.sections():
        inv = Kostal(section, get_ip(section), get_instance(section), get_password(section),
                     get_interval(section), get_position(section))
        logger.info('Found config: ' + section)
        logger.info(inv.inverter_name + ' at ' + inv.ip)
        inverters.append(inv)

        # Apply log level from first section that has it
        if not log_level_set and parser.has_option(section, 'loglevel'):
            from loggingConfig import set_log_level
            set_log_level(parser.get(section, 'loglevel'))
            log_level_set = True

    logger.info('Configured {} inverter(s)'.format(len(inverters)))
    return inverters


def set_dbus_data(inv, data):
    time_ms = int(round(time.time() * 1000))
    if inv.stats.last_time == time_ms:
        inv.dbus_inverter.inc('/stats/repeated_values')
        inv.dbus_inverter.inc('/stats/last_repeated_values')
        logger.info('[' + inv.inverter_name + '] got repeated value')
    else:
        inv.stats.last_time = time_ms
        inv.dbus_inverter.set('/stats/last_repeated_values', 0)

        inv.dbus_inverter.set('/Ac/Power', (data['PT']), 1)
        inv.dbus_inverter.set('/Ac/Current', (data['IN0']), 1)
        inv.dbus_inverter.set('/Ac/L1/Current', (data['IA']), 2)
        inv.dbus_inverter.set('/Ac/L1/Voltage', (data['VA']), 1)
        inv.dbus_inverter.set('/Ac/L1/Power', (data['PA']), 1)
        inv.dbus_inverter.set('/Ac/L1/Energy/Forward', data['EA'], 3)
        inv.dbus_inverter.set('/Ac/L2/Current', (data['IB']), 2)
        inv.dbus_inverter.set('/Ac/L2/Voltage', (data['VB']), 1)
        inv.dbus_inverter.set('/Ac/L2/Power', (data['PB']), 1)
        inv.dbus_inverter.set('/Ac/L2/Energy/Forward', data['EB'], 3)
        inv.dbus_inverter.set('/Ac/L3/Current', (data['IC']), 2)
        inv.dbus_inverter.set('/Ac/L3/Voltage', (data['VC']), 1)
        inv.dbus_inverter.set('/Ac/L3/Power', (data['PC']), 1)
        inv.dbus_inverter.set('/Ac/L3/Energy/Forward', data['EC'], 3)

        inv.dbus_inverter.set('/Ac/Energy/Forward', data['EFAT'], 3)

        inv.dbus_inverter.set('/Ac/Voltage', data['VA'], 1)
        inv.dbus_inverter.set('/Ac/Frequency', data['FREQ'], 2)

        if data.get('PV1_U') is not None:
            inv.dbus_inverter.set('/Dc/0/Voltage', data['PV1_U'], 1)
            inv.dbus_inverter.set('/Dc/0/Current', data['PV1_I'], 2)
            inv.dbus_inverter.set('/Dc/0/Power', data['PV1_P'], 1)
            inv.dbus_inverter.set('/Dc/1/Voltage', data['PV2_U'], 1)
            inv.dbus_inverter.set('/Dc/1/Current', data['PV2_I'], 2)
            inv.dbus_inverter.set('/Dc/1/Power', data['PV2_P'], 1)
            if inv.inv_settings['string_cnt'] >= 3:
                inv.dbus_inverter.set('/Dc/2/Voltage', data['PV3_U'], 1)
                inv.dbus_inverter.set('/Dc/2/Current', data['PV3_I'], 2)
                inv.dbus_inverter.set('/Dc/2/Power', data['PV3_P'], 1)

        if data.get('INV_STATE') is not None:
            inv.dbus_inverter.set('/StatusCode', KOSTAL_STATE_TO_STATUS.get(data['INV_STATE'], 10))

        logger.debug('[' + inv.inverter_name + '] ' + "POWER Phase A: " + str(data['PA']) + "W")
        logger.debug('[' + inv.inverter_name + '] ' + "POWER Phase B: " + str(data['PB']) + "W")
        logger.debug('[' + inv.inverter_name + '] ' + "POWER Phase C: " + str(data['PC']) + "W")
        logger.debug('[' + inv.inverter_name + '] ' + "POWER Total: " + str(data['PT']) + "W")
        logger.debug('[' + inv.inverter_name + '] ' + "Energy Total: " + str(data['EFAT']) + "kWh")


def init_session(inv):
    session_id, sw_version, api_version, inv_settings = get_session_key(inv.password, inv.ip)
    inv.sw_version = sw_version
    inv.inv_settings = inv_settings
    inv.session_id = session_id
    inv.dev_state = DevState.Connected
    if inv.dbus_inverter:
        inv.dbus_inverter.set('/Connected', 1)
    logger.info('[' + inv.inverter_name + '] Session initialized successfully')


def init_dbus(inv):
    s = inv.inv_settings
    inv.dbus_inverter = DbusInverter(inv.inverter_name, inv.ip, inv.instance, s['serial'],
                                     s['product_name'],
                                     inv.sw_version, '0.1', inv.position,
                                     max_power=s['max_power'], string_cnt=s['string_cnt'])


def invalidate_dbus_data(inv):
    inv.dbus_inverter.set('/Connected', 0)
    inv.dbus_inverter.set('/Ac/L1/Current', None)
    inv.dbus_inverter.set('/Ac/L2/Current', None)
    inv.dbus_inverter.set('/Ac/L3/Current', None)
    inv.dbus_inverter.set('/Ac/L1/Power', None)
    inv.dbus_inverter.set('/Ac/L2/Power', None)
    inv.dbus_inverter.set('/Ac/L3/Power', None)
    inv.dbus_inverter.set('/Ac/L1/Voltage', None)
    inv.dbus_inverter.set('/Ac/L2/Voltage', None)
    inv.dbus_inverter.set('/Ac/L3/Voltage', None)
    inv.dbus_inverter.set('/Ac/Power', None)
    inv.dbus_inverter.set('/Ac/Current', None)
    inv.dbus_inverter.set('/Ac/Voltage', None)
    inv.dbus_inverter.set('/Ac/Frequency', None)
    inv.dbus_inverter.set('/Dc/0/Voltage', None)
    inv.dbus_inverter.set('/Dc/0/Current', None)
    inv.dbus_inverter.set('/Dc/0/Power', None)
    inv.dbus_inverter.set('/Dc/1/Voltage', None)
    inv.dbus_inverter.set('/Dc/1/Current', None)
    inv.dbus_inverter.set('/Dc/1/Power', None)
    if inv.inv_settings['string_cnt'] >= 3:
        inv.dbus_inverter.set('/Dc/2/Voltage', None)
        inv.dbus_inverter.set('/Dc/2/Current', None)
        inv.dbus_inverter.set('/Dc/2/Power', None)
    inv.dbus_inverter.set('/StatusCode', None)


def reconnect(inv):
    # Exponential backoff: wait before reconnecting
    if inv.reconnect_delay > 0:
        logger.info('[' + inv.inverter_name + '] Waiting {}s before reconnect attempt'.format(inv.reconnect_delay))
        time.sleep(inv.reconnect_delay)

    try:
        logger.info('[' + inv.inverter_name + '] Trying to reconnect to ' + inv.ip)
        init_session(inv)
        inv.data_service.reset_energy_state()
        inv.stats.last_connection_errors = 0
        inv.reconnect_delay = 0
        logger.info('[' + inv.inverter_name + '] Reconnect successful')
        return True
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError) as err:
        logger.warning('[' + inv.inverter_name + '] Reconnect failed for ' + inv.ip + ': ' + str(err))
        inv.stats.connection_ko += 1
        inv.stats.last_connection_errors += 1
        # Increase backoff: 1, 2, 4, 8, 16, 32, 60, 60, ...
        if inv.reconnect_delay == 0:
            inv.reconnect_delay = 1
        else:
            inv.reconnect_delay = min(inv.reconnect_delay * 2, MAX_RECONNECT_DELAY)
        return False

def read_data(inv):
    try:
        logger.debug('[' + inv.inverter_name + '] reading data from ' +
              inv.inverter_name + ' inverter at ' + inv.ip + ' using sessionid ' + inv.session_id)
        data = inv.data_service.get_data(inv.ip, inv.session_id)
        set_dbus_data(inv, data)
        inv.stats.connection_ok += 1
        inv.stats.last_connection_errors = 0
        inv.dbus_inverter.set('/Connected', 1)
        logger.debug('[' + inv.inverter_name + '] done.')
        return
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError) as err:
        logger.warning('[' + inv.inverter_name + '] Error reading from ' + inv.ip + ': ' + str(err))
        inv.stats.connection_ko += 1
        inv.stats.last_connection_errors += 1
        return 1


def cyclic_update(inv, run_event):
    while run_event.is_set():
        logger.debug('[' + inv.inverter_name + '] Thread: doing')

        push_statistics(inv)

        if inv.stats.last_connection_errors > inv.max_retries:
            logger.warning('[' + inv.inverter_name + '] Lost connection to kostal, reset')
            inv.dev_state = DevState.Connect
            inv.stats.last_connection_errors = 0
            inv.stats.reconnect += 1
            invalidate_dbus_data(inv)

        elif inv.dev_state == DevState.Connected:
            read_data(inv)
        elif inv.dev_state == DevState.Connect:
            reconnect(inv)
        else:
            logger.error('[' + inv.inverter_name + '] invalid state...')

        time.sleep(inv.interval)
    return


mainloop = None


def shutdown(signum=None, frame=None):
    """Graceful shutdown handler for SIGTERM/SIGINT."""
    sig_name = signal.Signals(signum).name if signum else 'unknown'
    logger.info('Received ' + sig_name + ', shutting down...')
    run_event.clear()
    if mainloop is not None:
        mainloop.quit()


DBusGMainLoop(set_as_default=True)
inverters = parse_config()

for inv in inverters:
    try:
        init_session(inv)
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError) as err:
        logger.warning('[' + inv.inverter_name + '] Initial session setup failed for ' + inv.ip + ': ' + str(err))
        inv.dev_state = DevState.Connect
    init_dbus(inv)
    if inv.dev_state != DevState.Connected:
        invalidate_dbus_data(inv)

run_event = threading.Event()
run_event.set()

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

try:
    threads = []
    for inv in inverters:
        t = threading.Thread(target=cyclic_update, args=(inv, run_event), name='update-' + inv.inverter_name)
        t.daemon = True
        t.start()
        threads.append(t)

    mainloop = GLib.MainLoop()
    mainloop.run()

except (KeyboardInterrupt, SystemExit):
    pass
finally:
    run_event.clear()
    for t in threads:
        t.join(timeout=10)
    logger.info('Shutdown complete')
