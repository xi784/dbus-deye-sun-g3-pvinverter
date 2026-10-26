#!/usr/bin/env python

import platform
import logging
import os
import sys
import time
import configparser
from datetime import datetime

if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject

# Victron lib path
VICTRON_PATH = "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"
if os.path.isdir(VICTRON_PATH):
    sys.path.insert(1, VICTRON_PATH)

from vedbus import VeDbusService
from DeyeAtComm import DeyeAtComm


class DbusDeyeClusterService:
    PHASES = ("L1", "L2", "L3")

    def __init__(self, servicename, paths, productname='Deye Sun G3 Cluster', connection='Deye AT Commands Cluster'):
        config = self._getConfig()
        deviceinstance = int(config['DEFAULT']['Deviceinstance'])
        customname = config['DEFAULT']['CustomName']
        self.serial = 'Cluster'

        self._dbusservice = VeDbusService(
            "{}.tcp_{:02d}".format(servicename, deviceinstance)
        )
        self._paths = paths
        self._lastUpdate = 0

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Create management objects
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path(
            '/Mgmt/ProcessVersion',
            'Cluster v1.2, running on Python ' + platform.python_version()
        )
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', customname)
        self._dbusservice.add_path('/Connected', 0)

        self._dbusservice.add_path('/Latency', None)
        self._dbusservice.add_path('/FirmwareVersion', 'Multi')
        self._dbusservice.add_path('/HardwareVersion', 1.0)
        self._dbusservice.add_path('/Serial', self._getClusterSerial())
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/Position', int(config['DEFAULT']['Position']))
        self._dbusservice.add_path('/StatusCode', 0)

        # Add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings.get('initial', 0),
                gettextcallback=settings.get('textformat'),
                writeable=True,
                onchangecallback=self._handlechangedvalue
            )

        # Timers
        update_interval = int(config['DEFAULT']['UpdateInterval'])
        gobject.timeout_add(update_interval * 1000, self._update)
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    @staticmethod
    def _getConfig():
        config = configparser.ConfigParser()
        config_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.ini')
        config.read(config_file)
        return config

    def _getClusterSerial(self):
        config = self._getConfig()
        l1_addr = config['DEFAULT'].get('Address_L1', '')
        l2_addr = config['DEFAULT'].get('Address_L2', '')
        return "L1:{0}_L2:{1}".format(l1_addr[:6], l2_addr[:6])

    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config['DEFAULT'].get('SignOfLifeLog')
        return int(value) if value else 5

    def _poll_inverter(self, address, port, reset_daily):
        """Poll a single inverter with robust exception handling."""
        if not address:
            return None

        try:
            modbus = DeyeAtComm(address, port)
            serial = getattr(modbus, 'serial', '')

            if reset_daily:
                self._checkResetDailyProduction(modbus)

            energy = self._getDailyProduction(modbus)
            power = self._getTotalACOutputPower(modbus)
            current = self._getGridCurrent(modbus)
            voltage = self._getAcVoltage(modbus)
            fw = modbus.getversion()

            return {
                "energy": energy,
                "power": power,
                "current": current,
                "voltage": voltage,
                "fw": fw,
                "serial": serial,
                "online": True,
            }

        except Exception as e:
            logging.debug("Inverter at %s not reachable or offline: %s", address, e)
            return {
                "energy": 0.0,
                "power": 0.0,
                "current": 0.0,
                "voltage": 0.0,
                "fw": "?",
                "serial": "",
                "online": False,
            }

    def _getClusterData(self):
        config = self._getConfig()
        port = int(config['DEFAULT'].get('Port', 502))
        reset_daily = int(config['DEFAULT'].get('ResetDailyProduction', 0))

        phases = {
            'L1': config['DEFAULT'].get('Address_L1'),
            'L2': config['DEFAULT'].get('Address_L2'),
            'L3': config['DEFAULT'].get('Address_L3'),
        }

        cluster_data = {}
        any_online = False

        for phase, addr in phases.items():
            if addr:
                data = self._poll_inverter(addr, port, reset_daily)
                cluster_data[phase] = data if data else {
                    "energy": 0.0,
                    "power": 0.0,
                    "current": 0.0,
                    "voltage": 0.0,
                    "online": False,
                }
                if data and data.get('online'):
                    any_online = True
            else:
                cluster_data[phase] = {
                    "energy": 0.0,
                    "power": 0.0,
                    "current": 0.0,
                    "voltage": 0.0,
                    "online": False,
                }

        return cluster_data, any_online

    def _checkResetDailyProduction(self, modbus):
        try:
            old_values = modbus.read(register_addr=0x0016, count=3)
            if not old_values or len(old_values) < 3:
                return

            new_values = self._calcSystemTime()
            if old_values[0] != new_values[0] or old_values[1] / 256 != new_values[1] / 256:
                modbus.write(register_addr=0x0016, count=3, values=new_values)
        except Exception:
            pass

    def _calcSystemTime(self):
        now = datetime.now()
        ym = (now.year % 100) * 256 + now.month
        dh = now.day * 256 + now.hour
        ms = now.minute * 256 + now.second
        return [ym, dh, ms]

    def _getDailyProduction(self, modbus):
        result = modbus.read(0x3c, 1)
        if not result:
            return 0.0
        return float(result[0]) * 0.1

    def _getAcVoltage(self, modbus):
        result = modbus.read(0x49, 1)
        if not result:
            return 0.0
        return float(result[0]) * 0.1

    def _getGridCurrent(self, modbus):
        result = modbus.read(0x4C, 1)
        if not result:
            return 0.0
        return float(result[0]) * 0.1

    def _getTotalACOutputPower(self, modbus):
        values = modbus.read(register_addr=0x0056, count=2)
        if not values or len(values) < 2:
            return 0.0

        combined_bytes = values[1].to_bytes(2, 'big') + values[0].to_bytes(2, 'big')
        int_value = int.from_bytes(combined_bytes, 'big')
        return float(int_value) * 0.1

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s", self._lastUpdate)
        logging.info("Total Cluster Power (/Ac/Power): %s W", self._dbusservice['/Ac/Power'])
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            cluster_data, any_online = self._getClusterData()

            total_power = 0.0
            total_energy = 0.0
            max_voltage = 0.0
            total_current = 0.0

            for phase in self.PHASES:
                pre = '/Ac/' + phase
                pdata = cluster_data.get(phase, {})

                voltage = float(pdata.get('voltage', 0.0))
                current = float(pdata.get('current', 0.0))
                power = float(pdata.get('power', 0.0))
                energy = float(pdata.get('energy', 0.0))

                self._dbusservice[pre + '/Voltage'] = voltage
                self._dbusservice[pre + '/Current'] = current
                self._dbusservice[pre + '/Power'] = power
                self._dbusservice[pre + '/Energy/Forward'] = energy

                total_power += power
                total_energy += energy
                total_current += current

                if voltage > max_voltage:
                    max_voltage = voltage

            self._dbusservice['/Ac/Voltage'] = max_voltage
            self._dbusservice['/Ac/Current'] = total_current
            self._dbusservice['/Ac/Power'] = total_power
            self._dbusservice['/Ac/Energy/Forward'] = total_energy

            self._dbusservice['/Connected'] = 1 if any_online else 0
            self._lastUpdate = time.time()

        except Exception as e:
            logging.critical('Error in cluster _update: %s', e, exc_info=True)

        try:
            index = int(self._dbusservice['/UpdateIndex']) + 1
            if index > 255:
                index = 0
            self._dbusservice['/UpdateIndex'] = index
        except Exception:
            pass

        return True

    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s", path, value)
        return True


def main():
    config = DbusDeyeClusterService._getConfig()
    loglevel = config['DEFAULT'].get('LogLevel', 'INFO')
    numeric_level = getattr(logging, loglevel.upper(), logging.INFO)

    log_dir = os.path.dirname(os.path.realpath(__file__))
    logging.basicConfig(
        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        level=numeric_level,
        handlers=[
            logging.FileHandler(os.path.join(log_dir, "current.log")),
            logging.StreamHandler()
        ]
    )

    try:
        logging.info("Start Deye Cluster Service")

        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)

        def _kwh(p, v):
            return str(round(v, 2)) + 'kWh'
        def _a(p, v):
            return str(round(v, 1)) + 'A'
        def _w(p, v):
            return str(round(v, 1)) + 'W'
        def _v(p, v):
            return str(round(v, 1)) + 'V'

        DbusDeyeClusterService(
            servicename='com.victronenergy.pvinverter',
            paths={
                '/Ac/Energy/Forward': {'initial': None, 'textformat': _kwh},
                '/Ac/Power': {'initial': 0, 'textformat': _w},
                '/Ac/Current': {'initial': 0, 'textformat': _a},
                '/Ac/Voltage': {'initial': 0, 'textformat': _v},

                '/Ac/L1/Voltage': {'initial': 0, 'textformat': _v},
                '/Ac/L2/Voltage': {'initial': 0, 'textformat': _v},
                '/Ac/L3/Voltage': {'initial': 0, 'textformat': _v},
                '/Ac/L1/Current': {'initial': 0, 'textformat': _a},
                '/Ac/L2/Current': {'initial': 0, 'textformat': _a},
                '/Ac/L3/Current': {'initial': 0, 'textformat': _a},
                '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L1/Energy/Forward': {'initial': None, 'textformat': _kwh},
                '/Ac/L2/Energy/Forward': {'initial': None, 'textformat': _kwh},
                '/Ac/L3/Energy/Forward': {'initial': None, 'textformat': _kwh},
            }
        )

        logging.info('Connected to dbus, entering gobject.MainLoop()')
        mainloop = gobject.MainLoop()
        mainloop.run()

    except Exception as e:
        logging.critical('Error in main: %s', e, exc_info=True)


if __name__ == "__main__":
    main()
