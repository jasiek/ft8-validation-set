"""Stdlib-only MQTT 3.1.1 subscriber for the PSK Reporter live feed (mqtt.pskreporter.info).

Topic layout: pskr/filter/v2/{band}/{mode}/{sendercall}/{receivercall}/{senderloc}/{receiverloc}/{sendercty}/{receivercty}
Payload (JSON): sq, f (Hz), md, rp (SNR dB), t (spot unix time), t_tx (tx unix time, if present),
                sc/sl (sender call/locator), rc/rl (receiver call/locator), sa/ra (ADIF country), b (band)

  python3 tools/pskr.py OUT.jsonl SECONDS [topic ...]   (default topic: all FT8)
Each received spot is written as one JSON line with "rx_t" (our arrival time) added.
"""
import json, os, socket, struct, sys, time

HOST, PORT = "mqtt.pskreporter.info", 1883


def _varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s):
    b = s.encode()
    return struct.pack(">H", len(b)) + b


class MQTT:
    def __init__(self, host=HOST, port=PORT, keepalive=60, timeout=90):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.keepalive = keepalive
        cid = f"ft8val-{os.getpid()}-{int(time.time())}"
        var = _str("MQTT") + bytes([4, 0x02]) + struct.pack(">H", keepalive)  # clean session
        self._send(0x10, var + _str(cid))
        typ, body = self._recv()
        if typ != 0x20 or body[1] != 0:
            raise ConnectionError(f"CONNACK refused: {body!r}")
        self.last_tx = time.time()

    def _send(self, hdr, body):
        self.s.sendall(bytes([hdr]) + _varint(len(body)) + body)
        self.last_tx = time.time()

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            c = self.s.recv(n - len(buf))
            if not c:
                raise ConnectionError("connection closed")
            buf += c
        return buf

    def _recv(self):
        hdr = self._read(1)[0]
        n, mult = 0, 1
        while True:
            b = self._read(1)[0]
            n += (b & 0x7F) * mult
            mult <<= 7
            if not b & 0x80:
                break
        return hdr & 0xF0, (hdr & 0x0F, self._read(n)) if hdr & 0xF0 == 0x30 else self._read(n)

    def subscribe(self, topics):
        body = struct.pack(">H", 1) + b"".join(_str(t) + b"\x00" for t in topics)
        self._send(0x82, body)

    def messages(self):
        """Yield (topic, payload_bytes). Sends PINGREQ to keep the session alive."""
        self.s.settimeout(5)
        while True:
            if time.time() - self.last_tx > self.keepalive / 2:
                self._send(0xC0, b"")
            try:
                typ, body = self._recv()
            except socket.timeout:
                continue
            if typ == 0x30:
                flags, data = body
                tl = struct.unpack(">H", data[:2])[0]
                topic = data[2:2 + tl].decode("utf-8", "replace")
                pos = 2 + tl + (2 if (flags >> 1) & 3 else 0)  # skip packet id for QoS>0
                yield topic, data[pos:]


def capture(out_path, seconds, topics=("pskr/filter/v2/+/FT8/#",), log=print):
    until = time.time() + seconds
    n, backoff = 0, 5
    with open(out_path, "a") as fh:
        while time.time() < until:
            try:
                m = MQTT()
                m.subscribe(list(topics))
                log(f"[pskr] connected, subscribed {list(topics)}")
                backoff = 5
                last_report = time.time()
                for topic, payload in m.messages():
                    try:
                        d = json.loads(payload)
                    except ValueError:
                        continue
                    d["rx_t"] = round(time.time(), 2)
                    fh.write(json.dumps(d, separators=(",", ":")) + "\n")
                    n += 1
                    if time.time() - last_report > 60:
                        fh.flush()
                        log(f"[pskr] {n} spots so far")
                        last_report = time.time()
                    if time.time() >= until:
                        break
            except Exception as e:
                log(f"[pskr] {e!r}; reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
    log(f"[pskr] done: {n} spots -> {out_path}")
    return n


if __name__ == "__main__":
    capture(sys.argv[1], float(sys.argv[2]), sys.argv[3:] or ("pskr/filter/v2/+/FT8/#",))
