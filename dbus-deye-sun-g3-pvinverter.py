#!/usr/bin/env python

import platform
import logging
import os
import sys
import time
import socket
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


class DeyeAtComm:
    Logger_Init_CMD = b"WIFIKIT-214028-READ"

    def __init__(self, ip, port, timeout=1):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.settimeout(timeout)
        self.Logger_IP = ip
        self.Logger_Port = port
        self.ip = ""
        self.mac = ""
        self.serial = ""

        try:
            self.ip, self.mac, self.serial = self.hello()
        except Exception:
            logging.exception("Deye logger hello failed")
            raise

    def __del__(self):
        try:
            self.bye()
        except Exception:
            pass

    @staticmethod
    def modbus_crc(data):
        poly = 0xA001
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ poly
                else:
                    crc = crc >> 1
        return crc & 0xFFFF

    def build_request(self, register_addr, count, function, values=None):
        payload = bytearray([1, function])
        payload.extend(register_addr.to_bytes(2, "big"))
        payload.extend(count.to_bytes(2, "big"))

        if values is not None:
            payload.extend(len(values).to_bytes(2, "big"))
            for value in values:
                payload.extend(int(value).to_bytes(2, "big"))

        crc = self.modbus_crc(payload)
        payload.extend(crc.to_bytes(2, "little"))

        req_len = len(payload)
        cmd = "INVDATA={0},{1}".format(req_len, payload.hex())
        return ("AT+" + cmd + "\n").encode("ascii")

    def parse_at_response(self, rx_data):
        if not rx_data:
            raise ValueError("Empty response from Deye logger")

        text = rx_data.decode("ascii", errors="replace")
        text = text.replace("\x10", "").replace("\r", "").strip()

        if "+ok=" in text:
            payload_hex = text.split("+ok=", 1)[1]
        else:
            payload_hex = text

        if not payload_hex:
            raise ValueError("No payload in Deye logger response")

        payload = bytes.fromhex(payload_hex)

        if len(payload) < 5:
            raise ValueError("Response payload too short: {0!r}".format(payload))

        crc_rx = payload[-2:]
        crc_calc = self.modbus_crc(payload[:-2]).to_bytes(2, "little")
        if crc_rx != crc_calc:
            raise ValueError(
                "CRC mismatch: got {0}, expected {1}".format(
                    crc_rx.hex(), crc_calc.hex()
                )
            )

        rx_len = payload[2]

        if rx_len == 0:
            return []

        expected_len = rx_len + 5
        if len(payload) != expected_len:
            raise ValueError(
                "Unexpected payload length: got {0}, expected {1}. payload={2!r}".format(
                    len(payload), expected_len, payload
                )
            )

        values = []
        for idx in range(rx_len // 2):
            start = 3 + idx * 2
            word = payload[start:start + 2]
            values.append(int.from_bytes(word, "big"))

        return values

    def hello(self):
        self.sock.sendto(self.Logger_Init_CMD, (self.Logger_IP, self.Logger_Port))
        data = self.sock.recv(1024)

        text = data.decode("ascii", errors="replace")
        text = text.replace("\x10", "").replace("\r", "").strip()

        logging.debug("Hello response: %r", text)

        if "+ok=" in text:
            text = text.split("+ok=", 1)[1]

        if not text:
            raise ValueError("Logger hello response empty")

        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 3:
            raise ValueError("Unexpected hello response format: {0!r}".format(text))

        self.sock.sendto(b"+ok", (self.Logger_IP, self.Logger_Port))
        return parts[0], parts[1], parts[2]

    def bye(self):
        self.sock.sendto(b"AT+Q\n", (self.Logger_IP, self.Logger_Port))

    def getversion(self):
        self.sock.sendto(b"AT+YZVER\n", (self.Logger_IP, self.Logger_Port))
        repl = self.sock.recv(1024)
        text = repl.decode("ascii", errors="replace").replace("\x10", "").replace("\r", "").strip()
        if "+ok=" not in text:
            raise ValueError("getversion reply is not OK: {0!r}".format(text))
        version = text.split("+ok=", 1)[1].strip()
        return version

    def read(self, register_addr, count):
        request = self.build_request(register_addr, count, 0x03)
        logging.debug("Read request: %r", request)
        self.sock.sendto(request, (self.Logger_IP, self.Logger_Port))
        rx = self.sock.recv(1024)
        logging.debug("Read response: %r", rx)
        return self.parse_at_response(rx)

    def write(self, register_addr, count, values):
        request = self.build_request(register_addr, count, 0x10, values)
        logging.debug("Write request: %r", request)
        self.sock.sendto(request, (self.Logger_IP, self.Logger_Port))
        rx = self.sock.recv(1024)
        logging.debug("Write response: %r", rx)
        return self.parse_at_response(rx)


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

        logging.debug("%s /DeviceInstance = %d", servicename, deviceinstance)

        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path(
            '/Mgmt/ProcessVersion',
            'Cluster v1.2, running on Python ' + platform.python_version()
        )
        self._dbusservice.add_path('/Mgmt/Connection', connection)

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

        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings.get('initial', 0),
                gettextcallback=settings.get('textformat'),
                writeable=True,
                onchangecallback=self._handlechangedvalue
            )

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

        except Exception as exc:
            logging.debug("Inverter at %s not reachable or offline: %s", address, exc)
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

        except Exception as exc:
            logging.critical("Error in cluster _update: %s", exc, exc_info=True)

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

    except Exception as exc:
        logging.critical('Error in main: %s', exc, exc_info=True)


if __name__ == "__main__":
    main()
