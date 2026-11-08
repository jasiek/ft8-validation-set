"""Minimal stdlib-only KiwiSDR audio client (USB mode, uncompressed 16-bit PCM).

Writes 15-second WAV files aligned to FT8 slot boundaries (UTC :00/:15/:30/:45),
named YYMMDD_HHMMSS.wav so jt9 can decode them directly.
"""
import base64, os, socket, ssl, struct, sys, time, wave, json, random
from urllib.parse import urlparse

SLOT = 15
RATE = 12000


class WS:
    """Tiny RFC6455 client: enough for KiwiSDR (binary frames, ping/pong, masking)."""

    def __init__(self, host, port, path, secure=False, timeout=10):
        s = socket.create_connection((host, port), timeout=timeout)
        if secure:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"User-Agent: ft8-recorder\r\n\r\n")
        s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0]
        if b" 101 " not in status:
            raise ConnectionError(f"handshake failed: {status!r}")
        self.s = s

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, data, opcode=1):
        if isinstance(data, str):
            data = data.encode()
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.s.sendall(hdr + mask + masked)

    def recv(self):
        """Return one complete message payload (bytes)."""
        payload = b""
        while True:
            b0, b1 = self._read(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise ConnectionError(f"server closed: {data[2:]!r}")
            if op == 9:
                self.send(data, opcode=10)
                continue
            if op == 10:
                continue
            payload += data
            if fin:
                return payload

    def close(self):
        try:
            self.send(b"", opcode=8)
            self.s.close()
        except Exception:
            pass


class KiwiError(Exception):
    pass


def kiwi_stream(url, freq_khz, lo=200, hi=3000, timeout=10, info=None):
    """Generator yielding (arrival_time, samples_bytes_bigendian_int16, rssi_dbm)."""
    u = urlparse(url)
    host, secure = u.hostname, u.scheme == "https"
    port = u.port or (443 if secure else 80)
    base = u.path.rstrip("/")
    try:
        ws = WS(host, port, f"{base}/kiwi/{int(time.time())}/SND", secure=secure, timeout=timeout)
        ws.send("SET auth t=kiwi p=")
        first = ws.recv()
    except (TimeoutError, socket.timeout):
        ws = WS(host, port, f"{base}/{int(time.time())}/SND", secure=secure, timeout=timeout)
        ws.send("SET auth t=kiwi p=")
        first = ws.recv()
    try:
        last_ka = 0
        started = False
        pending = [first]
        while True:
            msg = pending.pop() if pending else ws.recv()
            now = time.time()
            if now - last_ka > 5:
                ws.send("SET keepalive")
                last_ka = now
            tag = msg[:3]
            if tag == b"MSG":
                text = msg[4:].decode("utf-8", "replace")
                for kv in text.split(" "):
                    k, _, v = kv.partition("=")
                    if k in ("too_busy", "badp", "down", "redirect", "kiwi_kick", "password_timeout"):
                        if v in ("0", ""):
                            continue
                        raise KiwiError(f"{k}={v}")
                    if k == "load_cfg" and info is not None:
                        from urllib.parse import unquote
                        try:
                            cfg = json.loads(unquote(v))
                            for key in ("inactivity_timeout_mins", "ip_limit_mins", "chan_no_pwd"):
                                if key in cfg:
                                    info[key] = cfg[key]
                        except Exception:
                            pass
                    if k == "audio_rate":
                        ws.send(f"SET AR OK in={int(float(v))} out=44100")
                    if k == "sample_rate" and not started:
                        rate = float(v)
                        if abs(rate - RATE) > 50:
                            raise KiwiError(f"unsupported sample rate {rate} (wideband kiwi?)")
                if not started and "audio_rate" in text:
                    started = True
                    for cmd in ("SET squelch=0 max=0", "SET genattn=0", "SET gen=0 mix=-1",
                                f"SET mod=usb low_cut={lo} high_cut={hi} freq={freq_khz:.3f}",
                                "SET agc=1 hang=0 thresh=-100 slope=6 decay=1000 manGain=50",
                                "SET compression=0", "SET ident_user=ft8-recorder"):
                        ws.send(cmd)
            elif tag == b"SND":
                flags = msg[3]
                if flags & 0x10:
                    raise KiwiError("server sent compressed audio")
                rssi = 0.1 * struct.unpack(">H", msg[8:10])[0] - 127
                yield now, msg[10:], rssi
    finally:
        ws.close()


def write_wav(path, pcm_le):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm_le)


def be_to_le(b):
    import array
    a = array.array("h")
    a.frombytes(b[: len(b) // 2 * 2])
    if sys.byteorder == "little":
        a.byteswap()
    return a.tobytes()


def record_slots(url, freq_khz, outdir, until, log=print, max_slots=None, info=None):
    """Record slot-aligned 15 s WAVs into outdir until wall-clock `until`.
    Reconnects on failure. Returns list of written filenames."""
    os.makedirs(outdir, exist_ok=True)
    written = []
    need = SLOT * RATE * 2
    backoff = 5
    while time.time() < until and (max_slots is None or len(written) < max_slots):
        try:
            t0 = None
            n = 0
            cur_slot, buf = None, bytearray()
            for arrival, data, rssi in kiwi_stream(url, freq_khz, info=info):
                pcm = be_to_le(data)
                ns = len(pcm) // 2
                if t0 is None:
                    # anchor: first sample of this chunk was produced ~ns/RATE before arrival
                    t0 = arrival - ns / RATE
                # chunk may straddle a slot boundary: split it
                pos = 0
                while pos < ns:
                    t = t0 + (n + pos) / RATE
                    slot = int(t // SLOT)
                    slot_end = (slot + 1) * SLOT
                    take = min(ns - pos, max(1, int(round((slot_end - t) * RATE))))
                    if slot != cur_slot:
                        if cur_slot is not None and len(buf) >= need * 0.9:
                            fn = time.strftime("%y%m%d_%H%M%S", time.gmtime(cur_slot * SLOT)) + ".wav"
                            write_wav(os.path.join(outdir, fn), bytes(buf[:need]).ljust(need, b"\0"))
                            written.append(fn)
                        cur_slot, buf = slot, bytearray()
                        # only keep slots we start at the very beginning of
                        if (t - slot * SLOT) > 0.2:
                            cur_slot = -slot  # mark partial; never long enough to save
                    if cur_slot == slot:
                        buf += pcm[pos * 2:(pos + take) * 2]
                    pos += take
                n += ns
                if time.time() >= until or (max_slots is not None and len(written) >= max_slots):
                    break
            backoff = 5
        except KiwiError:
            raise
        except Exception as e:
            log(f"[{url}] stream error: {e!r}; reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    return written
