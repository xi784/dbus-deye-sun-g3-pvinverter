import socket
import logging

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
            raise ValueError(f"Response payload too short: {payload!r}")

        crc_rx = payload[-2:]
        crc_calc = self.modbus_crc(payload[:-2]).to_bytes(2, "little")
        if crc_rx != crc_calc:
            raise ValueError(
                f"CRC mismatch: got {crc_rx.hex()}, expected {crc_calc.hex()}"
            )

        rx_len = payload[2]

        if rx_len == 0:
            return []

        expected_len = rx_len + 5
        if len(payload) != expected_len:
            raise ValueError(
                f"Unexpected payload length: got {len(payload)}, expected {expected_len}. payload={payload!r}"
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
            raise ValueError(f"Unexpected hello response format: {text!r}")

        self.sock.sendto(b"+ok", (self.Logger_IP, self.Logger_Port))
        return parts[0], parts[1], parts[2]

    def bye(self):
        self.sock.sendto(b"AT+Q\n", (self.Logger_IP, self.Logger_Port))

    def getversion(self):
        self.sock.sendto(b"AT+YZVER\n", (self.Logger_IP, self.Logger_Port))
        repl = self.sock.recv(1024)
        text = repl.decode("ascii", errors="replace").replace("\x10", "").replace("\r", "").strip()
        if "+ok=" not in text:
            raise ValueError(f"getversion reply is not OK: {text!r}")
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
