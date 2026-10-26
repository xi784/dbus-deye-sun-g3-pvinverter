#!/usr/bin/env python

# import normal packages
import platform
import logging
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import time
import configparser
from functools import reduce
from datetime import datetime

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__),
                '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService

from DeyeAtComm import DeyeAtComm

class DbusDeyeClusterService:
    def __init__(self, servicename, paths, productname='Deye Sun G3 Cluster', connection='Deye AT Commands Cluster'):
        config = self._getConfig()
        deviceinstance = int(config['DEFAULT']['Deviceinstance'])
        customname = config['DEFAULT']['CustomName']
        self.serial = 'Cluster'

        self._dbusservice = VeDbusService(
            "{}.tcp_{:02d}".format(servicename, deviceinstance))
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Create management objects
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path(
            '/Mgmt/ProcessVersion', 'Cluster v1.0, running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', customname)
        self._dbusservice.add_path('/Connected', 1)

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
                path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

        self._lastUpdate = 0

        # Timers
        updateInterval = int(config['DEFAULT']['UpdateInterval'])
        gobject.timeout_add(updateInterval * 1000, self._update)
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getClusterSerial(self):
        config = self._getConfig()
        return f"L1:{config['DEFAULT'].get('Address_L1','')[:6]}_L2:{config['DEFAULT'].get('Address_L2','')[:6]}"

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config

    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config['DEFAULT'].get('SignOfLifeLog')
        return int(value) if value else 5

    def _poll_inverter(self, address, port, reset_daily):
        """Fragt einen einzelnen Wechselrichter robust mit eigenem Try-Except ab."""
        if not address:
            return None
        
        try:
            modbus = DeyeAtComm(address, port)
            serial = modbus.serial
            
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
                "online": True
            }
        except Exception as e:
            # Im Debug-Modus loggen, damit nachts bei Timeouts nicht das Log zugemüllt wird
            logging.debug(f"Inverter at {address} not reachable or offline: {e}")
            return {
                "energy": 0.0,
                "power": 0.0,
                "current": 0.0,
                "voltage": 0.0,
                "fw": "?",
                "serial": "",
                "online": False
            }

    def _getClusterData(self):
        config = self._getConfig()
        port = int(config['DEFAULT']['Port'])
        reset_daily = int(config['DEFAULT'].get('ResetDailyProduction', 0))

        phases = {
            'L1': config['DEFAULT'].get('Address_L1'),
            'L2': config['DEFAULT'].get('Address_L2'),
            'L3': config['DEFAULT'].get('Address_L3')
        }

        cluster_data = {}
        any_online = False

        for phase, addr in phases.items():
            if addr:
                data = self._poll_inverter(addr, port, reset_daily)
                cluster_data[phase] = data
                if data and data['online']:
                    any_online = True
            else:
                cluster_data[phase] = {"energy": 0.0, "power": 0.0, "current": 0.0, "voltage": 0.0, "online": False}

        return cluster_data, any_online

    # Hilfsfunktionen für Register-Abfragen (aus Original übernommen[cite: 8])
    def _checkResetDailyProduction(self, modbus):
        try:
            oldValues = modbus.read(register_addr=0x0016, count=3)
            newValues = self._calcSystemTime()
            if oldValues[0] != newValues[0] or oldValues[1]/256 != newValues[1]/256:
                modbus.write(register_addr=0x0016, count=3, values=newValues)
        except Exception:
            pass

    def _calcSystemTime(self):
        now = datetime.now()
        ym = 256 * (now.year % 100) + now.month
        dh = 256 * now.day + now.hour
        ms = 256 * now.minute + now.second
        return [ym, dh, ms]

    def _getDailyProduction(self, modbus):
        result = modbus.read(0x3c, 1)
        return result[0] * 0.1

    def _getAcVoltage(self, modbus):
        result = modbus.read(0x49, 1)
        return result[0] * 0.1

    def _getGridCurrent(self, modbus):
        result = modbus.read(0x4C, 1)
        return result[0] * 0.1

    def _getTotalACOutputPower(self, modbus):
        values = modbus.read(register_addr=0x0056, count=2)
        byteValues = list(map(lambda v: v.to_bytes(2, 'big'), values))
        byteValues.reverse()
        bytes = reduce(lambda a, b: a + b, byteValues)
        intValue = int.from_bytes(bytes, 'big')
        return float(intValue) * 0.1

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Total Cluster Power (/Ac/Power): %s W" % (self._dbusservice['/Ac/Power']))
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            cluster_data, any_online = self._getClusterData()

            total_power = 0.0
            total_energy = 0.0
            max_voltage = 0.0
            total_current = 0.0

            for phase in ['L1', 'L2', 'L3']:
                pre = '/Ac/' + phase
                pdata = cluster_data.get(phase, {})

                self._dbusservice[pre + '/Voltage'] = pdata.get('voltage', 0.0)
                self._dbusservice[pre + '/Current'] = pdata.get('current', 0.0)
                self._dbusservice[pre + '/Power'] = pdata.get('power', 0.0)
                self._dbusservice[pre + '/Energy/Forward'] = pdata.get('energy', 0.0)

                total_power += pdata.get('power', 0.0)
                total_energy += pdata.get('energy', 0.0)
                total_current += pdata.get('current', 0.0)
                if pdata.get('voltage', 0.0) > max_voltage:
                    max_voltage = pdata.get('voltage', 0.0)

            # Gesamtsummen für den DBus setzen
            self._dbusservice['/Ac/Voltage'] = max_voltage
            self._dbusservice['/Ac/Current'] = total_current
            self._dbusservice['/Ac/Power'] = total_power
            self._dbusservice['/Ac/Energy/Forward'] = total_energy
            
            self._dbusservice['/Connected'] = 1 if any_online else 1 # Bleibt verbunden, auch nachts
            self._lastUpdate = time.time()

        except Exception as e:
            logging.critical('Error in cluster _update: %s', e, exc_info=True)

        try:
            index = self._dbusservice['/UpdateIndex'] + 1
            if index > 255:
                index = 0
            self._dbusservice['/UpdateIndex'] = index
        except Exception:
            pass

        return True

    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True


def main():
    config = DbusDeyeClusterService._getConfig(None)
    loglevel = config['DEFAULT'].get('LogLevel', 'INFO')
    numeric_level = getattr(logging, loglevel.upper(), logging.INFO)

    logging.basicConfig(format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        level=numeric_level,
                        handlers=[
                            logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                            logging.StreamHandler()
                        ])

    try:
        logging.info("Start Deye Cluster Service")

        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)

        def _kwh(p, v): return (str(round(v, 2)) + 'kWh')
        def _a(p, v): return (str(round(v, 1)) + 'A')
        def _w(p, v): return (str(round(v, 1)) + 'W')
        def _v(p, v): return (str(round(v, 1)) + 'V')

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
            })

        logging.info('Connected to dbus, entering gobject.MainLoop()')
        mainloop = gobject.MainLoop()
        mainloop.run()
    except Exception as e:
        logging.critical('Error in main: %s', e, exc_info=True)


if __name__ == "__main__":
    main()
