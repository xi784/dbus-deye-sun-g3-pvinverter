#!/usr/bin/env python
import logging
import configparser
import os
from DeyeAtComm import DeyeAtComm
from functools import reduce


def main():
    logging.basicConfig(format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        level=logging.INFO,
                        handlers=[
                            logging.FileHandler(
                                "%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                            logging.StreamHandler()
                        ])

    config = _getConfig()
    port = int(config['DEFAULT']['Port'])

    # Die drei Phasen-Adressen aus der neuen config.ini auslesen
    phases = {
        'L1': config['DEFAULT'].get('Address_L1'),
        'L2': config['DEFAULT'].get('Address_L2'),
        'L3': config['DEFAULT'].get('Address_L3')
    }

    logging.info("=== Starte Cluster-Konfigurations-Test ===")

    for phase, address in phases.items():
        if not address:
            logging.info(f"Phase {phase}: Keine IP konfiguriert, überspringe...")
            continue

        logging.info(f"\n--- Teste Wechselrichter auf {phase} (IP: {address}:{port}) ---")
        try:
            modbus = DeyeAtComm(address, port)
            serial = modbus.serial
            logging.info(f"Erfolgreich verbunden! Serial: {serial}")

            acEnergyForward = _getDailyProduction(modbus)
            acPower = _getTotalACOutputPower(modbus)
            acCurrent = _getGridCurrent(modbus)
            acVoltage = _getAcVoltage(modbus)
            firmwareVersion = modbus.getversion()

            result_data = {
                "phase": phase,
                "address": address,
                "serial": serial,
                "acEnergyForward": acEnergyForward,
                "acPower": acPower,
                "acCurrent": acCurrent,
                "acVoltage": acVoltage,
                "_firmwareVersion": firmwareVersion,
            }
            logging.info(f"Daten von Deye ({phase}):")
            logging.info(result_data)

        except Exception as e:
            logging.error(f"Fehler beim Abrufen von Phase {phase} ({address}): {e}")

    logging.info("\n=== Test abgeschlossen ===")


def _getDailyProduction(modbus):
    result = modbus.read(0x3c, 1)
    return result[0] * 0.1


def _getAcVoltage(modbus):
    result = modbus.read(0x49, 1)
    return result[0] * 0.1


def _getGridCurrent(modbus):
    result = modbus.read(0x4C, 1)
    return result[0] * 0.1


def _getTotalACOutputPower(modbus):
    values = modbus.read(register_addr=0x0056, count=2)
    byteValues = list(map(lambda v: v.to_bytes(2, 'big'), values))
    byteValues.reverse()
    bytes = reduce(lambda a, b: a + b, byteValues)
    intValue = int.from_bytes(bytes, 'big')
    value = float(intValue) * 0.1
    return value


def _getConfig():
    config = configparser.ConfigParser()
    config.read("%s/config.ini" %
                (os.path.dirname(os.path.realpath(__file__))))
    return config


if __name__ == "__main__":
    main()
