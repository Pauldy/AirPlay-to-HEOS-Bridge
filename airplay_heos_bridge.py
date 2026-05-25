#!/usr/bin/env python3
"""
airplay_heos_bridge.py
----------------------
Bridge AirPlay audio (received by shairport-sync) to a HEOS speaker via UPnP.

Architecture:
   iPhone/Mac (AirPlay client)
        |
        v   (RAOP / RTSP / RTP, mDNS-advertised by shairport-sync via Avahi)
   shairport-sync  --stdout-->  this script
                                  |
                                  | 1. SSDP-discover the HEOS speaker (M-SEARCH on 239.255.255.250:1900)
                                  | 2. Spin up a tiny HTTP server serving live WAV-stream-from-stdin at /stream.wav
                                  | 3. POST SOAP SetAVTransportURI + Play to the HEOS speaker's MediaRenderer
                                  v
                            HEOS speaker pulls the WAV stream over HTTP and plays it

Audio format from shairport-sync stdout backend: PCM 16-bit little-endian stereo 44100 Hz.
We wrap it in a WAV header with an "unknown / infinite" data length trick so HEOS keeps reading.

Usage:
    # 1. Find HEOS speakers and pick one (or pass --heos-ip directly)
    python3 airplay_heos_bridge.py --discover

    # 2. Run the bridge (in production, see the systemd unit at the bottom of this file)
    shairport-sync -o stdout | python3 airplay_heos_bridge.py --heos-ip 192.168.1.42
"""

import argparse
import http.server
import re
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.request
from collections import deque
from urllib.parse import urlparse

# --- Audio format from shairport-sync stdout backend ---
SAMPLE_RATE   = 44100
CHANNELS      = 2
BITS          = 16
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * (BITS // 8)  # 176400

HTTP_PORT     = 8123          # where this script serves the live audio
STREAM_PATH_WAV = "/stream.wav"
STREAM_PATH_MP3 = "/stream.mp3"

# Set at runtime by main() based on --mp3 flag
STREAM_PATH = STREAM_PATH_WAV
STREAM_CONTENT_TYPE = "audio/wav"
STREAM_SEND_WAV_HEADER = True

# ---------------------------------------------------------------------------
# 1. SSDP discovery
# ---------------------------------------------------------------------------
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

def ssdp_discover(timeout: float = 3.0, st: str = "urn:schemas-upnp-org:service:AVTransport:1"):
    """Send an SSDP M-SEARCH and yield (location_url, server_header) for each reply."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))

    seen = set()
    start = time.time()
    while time.time() - start < timeout:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            break
        text = data.decode("utf-8", errors="ignore")
        loc = re.search(r"LOCATION:\s*(\S+)", text, re.IGNORECASE)
        srv = re.search(r"SERVER:\s*(.+)", text, re.IGNORECASE)
        if loc and loc.group(1) not in seen:
            seen.add(loc.group(1))
            yield loc.group(1), (srv.group(1).strip() if srv else "")

def find_avtransport_control_url(device_description_url: str) -> str | None:
    """Fetch the device description XML and return the AVTransport controlURL (absolute)."""
    with urllib.request.urlopen(device_description_url, timeout=3) as r:
        xml = r.read().decode("utf-8", errors="ignore")

    # Find AVTransport service block, then its controlURL inside.
    m = re.search(
        r"<service>.*?<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>.*?"
        r"<controlURL>([^<]+)</controlURL>.*?</service>",
        xml, re.DOTALL,
    )
    if not m:
        return None
    control_url = m.group(1).strip()
    base = urlparse(device_description_url)
    if control_url.startswith("http"):
        return control_url
    if not control_url.startswith("/"):
        control_url = "/" + control_url
    return f"{base.scheme}://{base.netloc}{control_url}"

# ---------------------------------------------------------------------------
# 2. SOAP helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# HEOS CLI (telnet on TCP 1255) — preferred control path for HEOS speakers
# ---------------------------------------------------------------------------
import json

def heos_cli(heos_ip: str, command: str, timeout: float = 5.0) -> dict:
    """
    Send one HEOS CLI command and return the parsed JSON response.
    The HEOS CLI emits JSON lines terminated with \r\n. Some commands produce
    multiple lines (the response plus unsolicited event notifications); we
    consume until we find the line whose 'command' matches what we sent.
    """
    cmd_path = command.split("?", 1)[0].removeprefix("heos://")  # e.g. "player/play_stream"

    with socket.create_connection((heos_ip, 1255), timeout=timeout) as s:
        s.sendall((command + "\r\n").encode("ascii"))
        s.settimeout(timeout)
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            # Try to parse every complete line; return when we see our command echoed back.
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("heos", {}).get("command", "").endswith(cmd_path):
                    return msg
        raise TimeoutError(f"No matching response for {command}")

def _heos_cli_escape(value: str) -> str:
    """
    Escape a value for inclusion in a HEOS CLI command string.
    Per spec: '&' -> '&(&)', '=' -> '=(=)', '%' -> '%(%)'.
    Then we URL-encode anything else that's structurally problematic.
    """
    import urllib.parse as _up
    # Apply HEOS CLI's own escaping rules first
    value = value.replace("%", "%(%)").replace("&", "&(&)").replace("=", "=(=)")
    # URL-encode spaces and other URL-unsafe characters, but preserve the HEOS escapes
    # by quoting through safe-list
    return _up.quote(value, safe="()&=%")

def heos_play_stream(heos_ip: str, pid: int, stream_url: str,
                     artist: str | None = None,
                     album: str | None = None,
                     song: str | None = None,
                     station: str | None = None,
                     image_url: str | None = None) -> None:
    """
    Tell a HEOS player to play an HTTP audio stream URL, with optional metadata.

    The HEOS CLI play_stream command accepts optional sid/cid/mid/aid params for
    music-service streams, plus free-form 'name'/'station' for what the HEOS app
    displays as 'now playing'. Different firmware versions support different
    subsets of metadata fields; we send what we can and ignore failures gracefully.
    """
    if any(c in stream_url for c in "&=%"):
        raise ValueError(f"stream URL needs CLI-escaping; got {stream_url!r}")

    params = [f"pid={pid}", f"url={stream_url}"]
    if station:
        params.append(f"station={_heos_cli_escape(station)}")
    if song:
        params.append(f"name={_heos_cli_escape(song)}")
    if artist:
        params.append(f"artist={_heos_cli_escape(artist)}")
    if album:
        params.append(f"album={_heos_cli_escape(album)}")
    if image_url:
        params.append(f"image={_heos_cli_escape(image_url)}")

    cmd = "heos://browse/play_stream?" + "&".join(params)
    resp = heos_cli(heos_ip, cmd, timeout=8.0)
    if resp.get("heos", {}).get("result") != "success":
        # If metadata-laden command was rejected, fall back to bare URL form.
        if len(params) > 2:
            print(f"  play_stream with metadata failed ({resp.get('heos',{}).get('message')}); "
                  "retrying without metadata", file=sys.stderr)
            cmd = f"heos://browse/play_stream?pid={pid}&url={stream_url}"
            resp = heos_cli(heos_ip, cmd, timeout=8.0)
            if resp.get("heos", {}).get("result") != "success":
                raise RuntimeError(f"HEOS play_stream failed: {resp}")
        else:
            raise RuntimeError(f"HEOS play_stream failed: {resp}")

def heos_get_players(heos_ip: str) -> list[dict]:
    resp = heos_cli(heos_ip, "heos://player/get_players")
    if resp.get("heos", {}).get("result") != "success":
        raise RuntimeError(f"get_players failed: {resp}")
    return resp.get("payload", [])


class HeosClient:
    """
    Persistent connection to a HEOS speaker's CLI port.
    Reconnects on failure. Thread-safe for our use (one writer at a time).
    """
    def __init__(self, heos_ip: str, port: int = 1255):
        self.heos_ip = heos_ip
        self.port = port
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def _ensure_connected(self):
        if self._sock is not None:
            return
        s = socket.create_connection((self.heos_ip, self.port), timeout=5.0)
        s.settimeout(3.0)
        self._sock = s
        # Tell HEOS to stop sending unsolicited event notifications -- they'd
        # interleave with our command responses and complicate parsing.
        try:
            self._send_and_match("heos://system/register_for_change_events?enable=off",
                                 "system/register_for_change_events")
        except Exception:
            pass  # not fatal if it fails

    def _send_and_match(self, command: str, expect_path: str, timeout: float = 3.0) -> dict:
        assert self._sock is not None
        self._sock.sendall((command + "\r\n").encode("ascii"))
        self._sock.settimeout(timeout)
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("HEOS CLI closed the connection")
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("heos", {}).get("command", "").endswith(expect_path):
                    return msg
        raise TimeoutError(f"No matching response for {command}")

    def cmd(self, command: str, expect_path: str) -> dict:
        with self._lock:
            for attempt in range(2):
                try:
                    self._ensure_connected()
                    return self._send_and_match(command, expect_path)
                except (ConnectionError, OSError, TimeoutError) as e:
                    print(f"  HEOS CLI error ({e}); reconnecting…", file=sys.stderr)
                    self.close()
                    if attempt == 1:
                        raise
            raise RuntimeError("unreachable")

    def set_volume(self, pid: int, level: int):
        level = max(0, min(100, int(level)))
        self.cmd(f"heos://player/set_volume?pid={pid}&level={level}",
                 "player/set_volume")

    def set_mute(self, pid: int, mute: bool):
        state = "on" if mute else "off"
        self.cmd(f"heos://player/set_mute?pid={pid}&state={state}",
                 "player/set_mute")

    def close(self):
        if self._sock is not None:
            try: self._sock.close()
            except Exception: pass
            self._sock = None


# ---------------------------------------------------------------------------
# shairport-sync metadata pipe reader + volume translator
# ---------------------------------------------------------------------------
# shairport-sync emits metadata in a stream of tags. In practice, the format is
# messier than the documentation suggests:
#   - Whitespace (including \n) appears between tags inconsistently.
#   - Zero-length items omit the <data>...</data> entirely.
#   - During fast bursts (e.g. track changes), items get concatenated or even
#     interleaved at chunk boundaries.
#
# Rather than trying to match a whole <item>...</item> with one regex, we
# tokenize the stream: each <code>XXXX</code> and each <data...>BLOB</data>
# is extracted independently and paired by position. Codes without an
# intervening <data> before the NEXT code are treated as zero-length events.
# This is robust against weird whitespace, nesting, and partial junk.
import base64 as _b64

CODE_RE = re.compile(rb"<code>([0-9a-fA-F]+)</code>")
DATA_RE = re.compile(rb"<data encoding=\"base64\">\s*([A-Za-z0-9+/=\s]*?)\s*</data>",
                     re.DOTALL)

def _hex_to_fourcc(hexstr: bytes) -> str:
    try:
        return bytes.fromhex(hexstr.decode()).decode("ascii", errors="replace")
    except Exception:
        return ""

def _scan_metadata(buf: bytes) -> tuple[list[tuple[str, bytes | None]], bytes]:
    """
    Scan buf for (code, data) tuples. data is None for codes with no <data> block
    before the next code. Returns (items, remaining_buf) where remaining_buf is
    the trailing chunk we couldn't yet fully resolve (truncated tag at the end).
    """
    items: list[tuple[str, bytes | None]] = []
    # Find all code and data spans in order.
    code_iter = list(CODE_RE.finditer(buf))
    data_iter = list(DATA_RE.finditer(buf))
    if not code_iter:
        # No complete codes -- keep everything, waiting for more.
        return [], buf

    consumed_to = 0
    di = 0  # index into data_iter
    for i, cm in enumerate(code_iter):
        code = _hex_to_fourcc(cm.group(1))
        code_end = cm.end()
        next_code_start = code_iter[i+1].start() if i + 1 < len(code_iter) else len(buf)
        # Find a <data>...</data> block that starts AFTER this code and BEFORE the next.
        data: bytes | None = None
        while di < len(data_iter) and data_iter[di].start() < code_end:
            di += 1  # data block belongs to a previous code; skip
        if di < len(data_iter) and data_iter[di].start() < next_code_start:
            data = data_iter[di].group(1)
            consumed_to = data_iter[di].end()
            di += 1
        else:
            # No data for this code -- it's a zero-length event.
            consumed_to = cm.end()
        items.append((code, data))

    # The remainder might contain a partial code or data tag we haven't seen
    # the end of yet. Keep the buffer from consumed_to onward, but cap how much
    # we hold so a truly malformed stream can't OOM us. Cover art (PICT) can
    # be 200KB+ raw, ~270KB base64-encoded, so the cap needs to be generous.
    remainder = buf[consumed_to:]
    if len(remainder) > 2_000_000:
        remainder = remainder[-1_000_000:]
    return items, remainder

def airplay_to_heos_volume(airplay_volume: float) -> tuple[int, bool]:
    """
    Convert shairport-sync's 'airplay_volume' field to a HEOS (level, mute) pair.
      -144.0           -> muted (level unchanged conceptually; we return 0, True)
      -30.0 to 0.0     -> 0..100 with a perceptual (cube-root) curve
    """
    if airplay_volume <= -144.0 + 0.001:
        return 0, True
    # Clamp to documented range, then map.
    av = max(-30.0, min(0.0, airplay_volume))
    fraction = (av + 30.0) / 30.0           # 0.0 ... 1.0
    level = round(100.0 * (fraction ** (1.0 / 3.0)))
    return level, False

def parse_volume_payload(b64data: bytes) -> float | None:
    """Decode a 'pvol' data blob and return airplay_volume (first field)."""
    try:
        raw = _b64.b64decode(b64data, validate=False).decode("ascii", "replace")
        parts = raw.split(",")
        if len(parts) >= 1:
            return float(parts[0])
    except Exception as e:
        print(f"  pvol parse error: {e}", file=sys.stderr)
    return None

class MetadataBridge:
    """
    Reads shairport-sync metadata pipe. Two responsibilities:
      1. Volume changes (pvol) -> debounced HEOS set_volume / set_mute calls.
      2. Track metadata (asar/minm/asal) accumulated per track, applied by
         re-issuing play_stream with metadata args when forward_track_metadata
         is True. Re-issue causes ~1s of audio gap, so it's opt-in.

    Track changes are signaled by 'pbeg' (play begin). We accumulate metadata
    items between 'pbeg' events, then on the *next* pbeg (or after a quiet
    interval), apply the accumulated metadata.
    """
    def __init__(self, pipe_path: str, heos: HeosClient, pid: int,
                 heos_ip: str, stream_url: str,
                 use_linear: bool = False, debounce_ms: int = 200,
                 forward_track_metadata: bool = False):
        self.pipe_path = pipe_path
        self.heos = heos
        self.pid = pid
        self.heos_ip = heos_ip
        self.stream_url = stream_url
        self.use_linear = use_linear
        self.debounce = debounce_ms / 1000.0
        self.forward_track_metadata = forward_track_metadata

        # Volume state
        self._vol_target: tuple[int, bool] | None = None
        self._vol_last_applied: tuple[int, bool] | None = None
        self._cond = threading.Condition()
        self._stopped = False

        # Per-track metadata accumulator. 'pbeg' arrives at the START of a new
        # track, followed by the metadata items. 'mden' (metadata end) signals
        # that the bundle is complete and we should act on it.
        self._current_track: dict[str, str] = {}
        self._last_applied_track: dict[str, str] = {}

    def start(self):
        threading.Thread(target=self._reader_loop, name="meta-reader", daemon=True).start()
        threading.Thread(target=self._vol_applier_loop, name="vol-applier", daemon=True).start()

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    # ---- volume ----
    def _airplay_to_heos(self, airplay_volume: float) -> tuple[int, bool]:
        if self.use_linear:
            if airplay_volume <= -144.0 + 0.001:
                return 0, True
            av = max(-30.0, min(0.0, airplay_volume))
            return round((av + 30.0) / 30.0 * 100.0), False
        return airplay_to_heos_volume(airplay_volume)

    def _queue_volume(self, target: tuple[int, bool]):
        with self._cond:
            self._vol_target = target
            self._cond.notify_all()

    def _vol_applier_loop(self):
        while True:
            with self._cond:
                while self._vol_target is None and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                target = self._vol_target
                self._vol_target = None
            if target != self._vol_last_applied:
                level, mute = target
                try:
                    if self._vol_last_applied is None or self._vol_last_applied[1] != mute:
                        self.heos.set_mute(self.pid, mute)
                    if not mute:
                        self.heos.set_volume(self.pid, level)
                    self._vol_last_applied = target
                    print(f"  vol -> HEOS level={level} mute={mute}", file=sys.stderr)
                except Exception as e:
                    print(f"  vol apply failed: {e}", file=sys.stderr)
            time.sleep(self.debounce)

    # ---- track metadata ----
    def _apply_track_metadata(self):
        """
        Publish the current track to ICY_STATE so the HTTP handler injects it
        as a Shoutcast StreamTitle. If forward_track_metadata is True, ALSO
        re-issue play_stream (legacy/fallback path that causes audio gap).
        """
        track = dict(self._current_track)
        if not track or track == self._last_applied_track:
            return
        artist = track.get("asar")
        song = track.get("minm")
        album = track.get("asal")
        if not (artist or song):
            return

        # Always update Icy state -- the HTTP handler will inject only if a
        # speaker actually requested icy-metadata, so this is free for clients
        # that don't.
        title = ICY_STATE.set_track(artist, song, album)
        print(f"  track -> {artist!r} / {song!r} / {album!r} (icy='{title}')",
              file=sys.stderr)

        self._last_applied_track = track

        # Legacy: also re-issue play_stream. Now BARE -- no metadata args, since
        # HEOS mangles the URL when given any (appends them with '&' instead of '?').
        # The 1-second audio gap of this path is its only purpose now; metadata
        # delivery happens via Icy inline.
        if self.forward_track_metadata:
            try:
                heos_play_stream(self.heos_ip, self.pid, self.stream_url)
            except Exception as e:
                print(f"  metadata re-issue failed: {e}", file=sys.stderr)

    def _apply_cover_art(self, image_bytes: bytes):
        """Store cover art in ICY_STATE; will be advertised on next icy injection."""
        if not image_bytes:
            return
        # Sniff JPEG vs PNG by magic bytes
        if image_bytes.startswith(b"\x89PNG"):
            mime = "image/png"
        else:
            mime = "image/jpeg"
        h = ICY_STATE.set_cover(image_bytes, mime)
        print(f"  cover -> {len(image_bytes)} bytes {mime} (hash={h})", file=sys.stderr)

    # ---- pipe reader ----
    def _reader_loop(self):
        first_open = True
        while not self._stopped:
            try:
                f = open(self.pipe_path, "rb")
                break
            except FileNotFoundError:
                time.sleep(0.5)
            except Exception as e:
                print(f"  metadata pipe open error: {e}; retrying…", file=sys.stderr)
                time.sleep(1.0)
        else:
            return

        if first_open:
            print(f"  reading shairport metadata from {self.pipe_path}", file=sys.stderr)
            first_open = False
        buf = b""
        try:
            while not self._stopped:
                chunk = f.read(4096)
                if not chunk:
                    f.close()
                    time.sleep(0.5)
                    try:
                        f = open(self.pipe_path, "rb")
                    except Exception:
                        return
                    continue
                buf += chunk
                items, buf = _scan_metadata(buf)
                for code, data in items:
                    self._handle_item(code, data)
        finally:
            try: f.close()
            except Exception: pass

    def _handle_item(self, code: str, data: bytes | None):
        # Volume
        if code == "pvol" and data:
            av = parse_volume_payload(data)
            if av is not None:
                self._queue_volume(self._airplay_to_heos(av))
            return

        # Track metadata core DAAP/iTunes codes
        if code in ("asar", "minm", "asal", "asgn", "astn"):
            if data:
                try:
                    decoded = _b64.b64decode(data, validate=False).decode("utf-8", "replace")
                except Exception:
                    decoded = ""
                self._current_track[code] = decoded
            return

        # Cover art -- raw image bytes (JPEG/PNG), base64-encoded in the pipe.
        # Only present if shairport-sync.conf has include_cover_art = "yes".
        if code == "PICT" and data:
            try:
                image_bytes = _b64.b64decode(data, validate=False)
                self._apply_cover_art(image_bytes)
            except Exception as e:
                print(f"  cover decode error: {e}", file=sys.stderr)
            return

        # Metadata bundle complete -- apply now (shairport sends 'mden' at the
        # end of each metadata batch).
        if code == "mden":
            self._apply_track_metadata()
            return

        # Track boundary: 'pbeg' (play begin) means a new track is starting.
        # Reset accumulator; metadata for the new track follows shortly.
        if code == "pbeg":
            self._current_track = {}
            return

        # End of playback or flush -- clear state so next start gets fresh metadata.
        if code in ("pend", "pfls"):
            self._current_track = {}
            self._last_applied_track = {}
            return


# Backward-compatible alias for the older name used in earlier diffs.
VolumeBridge = MetadataBridge


# ---------------------------------------------------------------------------
# UPnP AVTransport (legacy fallback — kept for AVRs and non-HEOS renderers)
# ---------------------------------------------------------------------------
AVT_NS = "urn:schemas-upnp-org:service:AVTransport:1"

def soap_call(control_url: str, action: str, body_inner: str) -> bytes:
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body_inner}</s:Body></s:Envelope>'
    ).encode("utf-8")
    req = urllib.request.Request(
        control_url,
        data=envelope,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{AVT_NS}#{action}"',
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read()

def heos_play_url(control_url: str, stream_url: str) -> None:
    set_uri = (
        f'<u:SetAVTransportURI xmlns:u="{AVT_NS}">'
        '<InstanceID>0</InstanceID>'
        f'<CurrentURI>{stream_url}</CurrentURI>'
        '<CurrentURIMetaData></CurrentURIMetaData>'
        '</u:SetAVTransportURI>'
    )
    soap_call(control_url, "SetAVTransportURI", set_uri)

    play = (
        f'<u:Play xmlns:u="{AVT_NS}">'
        '<InstanceID>0</InstanceID><Speed>1</Speed>'
        '</u:Play>'
    )
    soap_call(control_url, "Play", play)

# ---------------------------------------------------------------------------
# 3. Live WAV streaming HTTP server
# ---------------------------------------------------------------------------
def make_wav_header() -> bytes:
    """
    Build a WAV header that claims a near-infinite data length.
    We use 0xFFFFFFFF, which most players treat as "stream of unknown length".
    """
    byte_rate   = BYTES_PER_SEC
    block_align = CHANNELS * (BITS // 8)
    data_len    = 0xFFFFFFFF       # ~unknown / infinite
    riff_size   = 0xFFFFFFFF
    return b"".join([
        b"RIFF", struct.pack("<I", riff_size), b"WAVE",
        b"fmt ", struct.pack("<I", 16),
        struct.pack("<HHIIHH", 1, CHANNELS, SAMPLE_RATE, byte_rate, block_align, BITS),
        b"data", struct.pack("<I", data_len),
    ])

class StdinFanout:
    """
    Reads PCM from sys.stdin.buffer in a background thread and delivers it
    to any number of subscriber queues (one per active HTTP client).

    The reader thread is NOT a daemon -- daemon threads blocked on stdin
    cause _enter_buffered_busy panics during interpreter shutdown. Instead
    we set a stop flag and rely on stdin EOF (which happens when the upstream
    pipe -- shairport-sync -- closes) plus an explicit close on Ctrl-C.
    """
    def __init__(self):
        self._subs: list[deque[bytes]] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stopped = False
        self._thread = threading.Thread(target=self._reader, name="stdin-fanout")
        self._thread.start()

    def _reader(self):
        # Use a small fixed chunk size (4 KiB). For raw PCM that's ~23ms of
        # audio per chunk -- low latency. For 192 kbps MP3 it's ~170ms, still
        # fine. Using a small fixed value keeps both code paths snappy without
        # hardcoding assumptions about the input format.
        chunk_size = 4096
        try:
            while not self._stopped:
                data = sys.stdin.buffer.read(chunk_size)
                if not data:           # EOF -- shairport-sync exited
                    break
                with self._cond:
                    for q in self._subs:
                        q.append(data)
                    self._cond.notify_all()
        finally:
            with self._cond:
                self._stopped = True
                self._cond.notify_all()

    def stop(self):
        """Best-effort: tell the reader to exit. Closing stdin unblocks read()."""
        self._stopped = True
        try:
            sys.stdin.buffer.close()
        except Exception:
            pass

    def subscribe(self) -> deque[bytes]:
        q: deque[bytes] = deque()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def wait(self, q) -> bool:
        """Block until q has data or the reader has stopped. Returns False on stop+empty."""
        with self._cond:
            while not q and not self._stopped:
                self._cond.wait()
            return bool(q)


# Created lazily inside main() so that --discover doesn't spin up a stdin reader.
FANOUT: StdinFanout | None = None


# ---------------------------------------------------------------------------
# Icy/Shoutcast metadata injection
# ---------------------------------------------------------------------------
# Shoutcast/Icecast metadata protocol:
#   - Client GET with header "Icy-MetaData: 1" advertises it accepts metadata.
#   - Server responds with header "icy-metaint: N" (e.g. 16384) and streams audio.
#   - After every N bytes of audio, server inserts a metadata block:
#       1 byte:  length of metadata block in 16-byte units (0 if unchanged)
#       N bytes: metadata text, format "StreamTitle='Artist - Title';" then
#                NUL-padded to a multiple of 16 bytes.
#   - Optional extension: "StreamUrl='http://.../cover.jpg';" for album art.
#
# HEOS firmware reads StreamTitle reliably (internet radio uses it). StreamUrl
# is undocumented for HEOS -- some firmware honors it, some doesn't.
ICY_METAINT = 16384  # 16 KiB between metadata blocks at 176400 B/s -> ~93ms cadence

class IcyState:
    """
    Thread-safe holder for current track metadata + cover art.
    The metadata bridge writes; the HTTP handler reads on every metadata-block
    injection. Cover art is bytes (JPEG/PNG), exposed at /cover/<hash>.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._title: str = ""           # "Artist - Title" or just title
        self._cover_bytes: bytes = b""
        self._cover_mime: str = "image/jpeg"
        self._cover_hash: str = ""

    def set_track(self, artist: str | None, song: str | None, album: str | None):
        """Set the current track. Returns the new title string."""
        parts = [p for p in (artist, song) if p]
        title = " - ".join(parts) if parts else (album or "")
        with self._lock:
            self._title = title
        return title

    def set_cover(self, image_bytes: bytes, mime: str = "image/jpeg"):
        import hashlib
        h = hashlib.sha1(image_bytes).hexdigest()[:16]
        with self._lock:
            self._cover_bytes = image_bytes
            self._cover_mime = mime
            self._cover_hash = h
        return h

    def snapshot(self) -> tuple[str, str]:
        """Return (title, cover_hash). Either may be empty."""
        with self._lock:
            return self._title, self._cover_hash

    def get_cover(self, h: str) -> tuple[bytes, str] | None:
        with self._lock:
            if h == self._cover_hash and self._cover_bytes:
                return self._cover_bytes, self._cover_mime
        return None

ICY_STATE = IcyState()

def build_icy_metadata(title: str, cover_url: str = "") -> bytes:
    """
    Build a Shoutcast metadata block: a 1-byte length-in-16s prefix followed
    by that many bytes of text, NUL-padded.
    """
    # Single-quote SHALL be escaped per convention by leaving it raw; most
    # clients tolerate apostrophes in titles. We avoid embedded semicolons
    # by replacing them in the input.
    safe_title = title.replace(";", ",").replace("\x00", "")
    parts = [f"StreamTitle='{safe_title}';"]
    if cover_url:
        safe_url = cover_url.replace(";", ",").replace("\x00", "")
        parts.append(f"StreamUrl='{safe_url}';")
    body = "".join(parts).encode("utf-8", errors="replace")
    # Pad to a multiple of 16 bytes, max 255*16 = 4080 bytes.
    if len(body) > 4080:
        body = body[:4080]
    padded_len = (len(body) + 15) // 16 * 16
    body = body + b"\x00" * (padded_len - len(body))
    return bytes([padded_len // 16]) + body

ICY_EMPTY_BLOCK = b"\x00"   # "no metadata change"


class WavStreamHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quiet by default; uncomment for debugging
        # sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
        pass

    def _normalize_path(self) -> str:
        """
        Return the effective path, tolerating broken URL composition.
        Some HEOS firmware appends '&key=value' suffixes to the stream URL with
        no '?' delimiter, producing requests like "/stream.mp3&station=AirPlay".
        Treat anything before the first '?' or '&' as the path.
        """
        p = self.path
        for sep in ("?", "&"):
            if sep in p:
                p = p.split(sep, 1)[0]
                break
        return p

    def do_HEAD(self):
        norm = self._normalize_path()
        if norm == STREAM_PATH:
            self.send_response(200)
            self.send_header("Content-Type", STREAM_CONTENT_TYPE)
            self.send_header("icy-name", "AirPlay")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if norm.startswith("/cover/"):
            h = norm[len("/cover/"):].split(".")[0]
            entry = ICY_STATE.get_cover(h)
            if entry is None:
                self.send_error(404); return
            data, mime = entry
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            return
        self.send_error(404)

    def do_GET(self):
        norm = self._normalize_path()
        # Cover-art endpoint
        if norm.startswith("/cover/"):
            h = norm[len("/cover/"):].split(".")[0]
            entry = ICY_STATE.get_cover(h)
            if entry is None:
                self.send_error(404); return
            data, mime = entry
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # Audio stream endpoint -- tolerate URL-mangling clients
        if norm != STREAM_PATH:
            print(f"  HTTP 404 for path={self.path!r} (normalized={norm!r})",
                  file=sys.stderr)
            self.send_error(404); return
        if FANOUT is None:
            self.send_error(503, "audio pipeline not started"); return

        # Did the client ask for Icy metadata? HEOS sends "Icy-MetaData: 1".
        icy_wanted = self.headers.get("Icy-MetaData", "0").strip() == "1"
        print(f"  HTTP GET {self.path} from {self.client_address[0]} "
              f"icy={'yes' if icy_wanted else 'no'}", file=sys.stderr)

        self.send_response(200)
        self.send_header("Content-Type", STREAM_CONTENT_TYPE)
        self.send_header("icy-name", "AirPlay")
        self.send_header("icy-pub", "0")
        if icy_wanted:
            self.send_header("icy-metaint", str(ICY_METAINT))
        self.send_header("Connection", "close")
        # No Content-Length -> let the speaker read until we close.
        self.end_headers()

        try:
            if STREAM_SEND_WAV_HEADER:
                self.wfile.write(make_wav_header())
            q = FANOUT.subscribe()
            try:
                if icy_wanted:
                    self._stream_with_icy(q)
                else:
                    self._stream_plain(q)
            finally:
                FANOUT.unsubscribe(q)
        except (BrokenPipeError, ConnectionResetError):
            pass  # speaker disconnected; that's fine

    def _stream_plain(self, q):
        while FANOUT.wait(q):
            while q:
                self.wfile.write(q.popleft())

    def _stream_with_icy(self, q):
        """
        Write audio bytes with an Icy metadata block injected every
        ICY_METAINT bytes. Each block carries the current StreamTitle
        (and optional StreamUrl for cover art) -- or a single zero byte
        if the title hasn't changed since the last injection.
        """
        bytes_until_meta = ICY_METAINT
        last_title = ""
        last_cover_hash = ""

        while FANOUT.wait(q):
            while q:
                chunk = q.popleft()
                # Emit chunk in pieces of <= bytes_until_meta until exhausted,
                # interleaving metadata blocks at the boundary.
                offset = 0
                while offset < len(chunk):
                    take = min(bytes_until_meta, len(chunk) - offset)
                    self.wfile.write(chunk[offset:offset + take])
                    offset += take
                    bytes_until_meta -= take
                    if bytes_until_meta == 0:
                        title, cover_hash = ICY_STATE.snapshot()
                        if title != last_title or cover_hash != last_cover_hash:
                            cover_url = ""
                            if cover_hash:
                                # Build absolute URL the speaker can fetch.
                                cover_url = self._cover_url_for(cover_hash)
                            self.wfile.write(build_icy_metadata(title, cover_url))
                            last_title = title
                            last_cover_hash = cover_hash
                        else:
                            self.wfile.write(ICY_EMPTY_BLOCK)
                        bytes_until_meta = ICY_METAINT

    def _cover_url_for(self, h: str) -> str:
        """Build the cover URL the SPEAKER will use to fetch art -- must match
        the Host the speaker connected to, not the loopback address."""
        host_hdr = self.headers.get("Host")
        if host_hdr:
            return f"http://{host_hdr}/cover/{h}.jpg"
        # Fallback: use the server's bound address
        host, port = self.server.server_address
        return f"http://{host}:{port}/cover/{h}.jpg"


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

# ---------------------------------------------------------------------------
# 4. Utility: figure out our LAN IP so HEOS knows where to fetch the stream
# ---------------------------------------------------------------------------
def local_ip_toward(remote_ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_ip, 1))
        return s.getsockname()[0]
    finally:
        s.close()

# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------
def cmd_discover(args):
    """Subcommand: scan the LAN for UPnP devices and identify HEOS ones."""
    print("Searching for UPnP root devices (3s)…", file=sys.stderr)
    seen_hosts: dict[str, tuple[str, str]] = {}
    for st in ("upnp:rootdevice", "ssdp:all"):
        for loc, srv in ssdp_discover(timeout=3.0, st=st):
            host = urlparse(loc).hostname or "?"
            seen_hosts.setdefault(host, (loc, srv))
    if not seen_hosts:
        print("  (none found — check firewall / same subnet)", file=sys.stderr)
        return 0
    for host, (loc, srv) in sorted(seen_hosts.items()):
        blob = (srv + " " + loc).lower()
        is_heos = any(k in blob for k in ("heos", "denon", "marantz"))
        tag = "[HEOS]" if is_heos else "      "
        print(f"  {tag} {host:15s}  {loc}")
        print(f"         server: {srv}")
    print("\nNext: pick a HEOS host and run `probe <its LOCATION url>` to see "
          "what services it offers, or `list-players --heos-ip <ip>`.", file=sys.stderr)
    return 0


def cmd_probe(args):
    """Subcommand: fetch a UPnP device description and dump its services."""
    print(f"Fetching {args.url} …", file=sys.stderr)
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(args.url, timeout=5, context=ctx) as r:
            final_url = r.geturl()
            ctype = r.headers.get("Content-Type", "")
            server = r.headers.get("Server", "")
            body = r.read()
    except Exception as e:
        print(f"  probe failed: {e}", file=sys.stderr)
        return 1

    if final_url != args.url:
        print(f"  -> redirected to: {final_url}", file=sys.stderr)
    print(f"  Server header: {server or '(none)'}", file=sys.stderr)
    print(f"  Content-Type:  {ctype or '(none)'}", file=sys.stderr)

    xml = body.decode("utf-8", errors="ignore")
    if "<root" not in xml or "urn:schemas-upnp-org:device" not in xml:
        print("\n  This does NOT look like a UPnP device description.", file=sys.stderr)
        print("  First 400 bytes of response:", file=sys.stderr)
        print("  " + repr(body[:400]), file=sys.stderr)
        print("\n  Likely culprits:", file=sys.stderr)
        print("   - router / firewall / NAS admin UI (often http->https w/ self-signed cert)",
              file=sys.stderr)
        print("   - reverse proxy or VPN gateway", file=sys.stderr)
        print("   - wrong IP — try `discover` from the SAME subnet as the speaker",
              file=sys.stderr)
        return 0

    for m in re.finditer(r"<deviceType>([^<]+)</deviceType>", xml):
        print(f"  deviceType:   {m.group(1)}")
    for m in re.finditer(r"<friendlyName>([^<]+)</friendlyName>", xml):
        print(f"  friendlyName: {m.group(1)}")
    for m in re.finditer(r"<manufacturer>([^<]+)</manufacturer>", xml):
        print(f"  manufacturer: {m.group(1)}")
    for m in re.finditer(r"<modelName>([^<]+)</modelName>", xml):
        print(f"  modelName:    {m.group(1)}")
    print("  services:")
    for m in re.finditer(
        r"<service>\s*<serviceType>([^<]+)</serviceType>.*?"
        r"<controlURL>([^<]+)</controlURL>.*?</service>",
        xml, re.DOTALL,
    ):
        stype, ctrl = m.group(1).strip(), m.group(2).strip()
        print(f"    - {stype}")
        print(f"        controlURL: {ctrl}")
    if re.search(r"<deviceList>", xml):
        print("  (embedded sub-devices present — HEOS often nests MediaRenderer here)")
    return 0


def cmd_list_players(args):
    """Subcommand: connect to a HEOS device's CLI and print all players on its account."""
    print(f"Connecting to HEOS CLI at {args.heos_ip}:1255 …", file=sys.stderr)
    players = heos_get_players(args.heos_ip)
    if not players:
        print("  (no players returned)", file=sys.stderr)
        return 0
    for p in players:
        print(f"  pid={p.get('pid')}  name={p.get('name')!r}  "
              f"model={p.get('model')}  ip={p.get('ip')}  net={p.get('network')}")
    return 0


def _resolve_pid(heos_ip: str, explicit_pid: int | None) -> int:
    """Pick a HEOS player pid: use --pid if given, else the one at heos_ip, else fail."""
    if explicit_pid is not None:
        return explicit_pid
    players = heos_get_players(heos_ip)
    if not players:
        sys.exit("HEOS CLI returned no players; is this device set up in the HEOS app?")
    if len(players) > 1:
        print("Multiple players found:", file=sys.stderr)
        for p in players:
            print(f"  {p}", file=sys.stderr)
        here = [p for p in players if p.get("ip") == heos_ip]
        if not here:
            sys.exit("Couldn't pick a player; pass --pid explicitly.")
        pid = here[0]["pid"]
    else:
        pid = players[0]["pid"]
    print(f"Using HEOS player pid={pid} ({players[0].get('name')!r})", file=sys.stderr)
    return pid


def _resolve_upnp_control_url(heos_ip: str, heos_port: int) -> str:
    """Find the AVTransport control URL on a UPnP renderer (legacy mode)."""
    desc_url = f"http://{heos_ip}:{heos_port}/upnp/desc/aios_device/aios_device.xml"
    try:
        control_url = find_avtransport_control_url(desc_url)
    except Exception:
        control_url = None
    if not control_url:
        print(f"Couldn't read {desc_url}; falling back to SSDP discovery…", file=sys.stderr)
        for loc, _ in ssdp_discover(timeout=3.0,
                                    st="urn:schemas-upnp-org:device:MediaRenderer:1"):
            if urlparse(loc).hostname == heos_ip:
                control_url = find_avtransport_control_url(loc)
                if control_url:
                    break
    if not control_url:
        sys.exit(f"Could not find AVTransport control URL on {heos_ip}")
    return control_url


def cmd_bridge(args):
    """Subcommand: the main event — run the AirPlay-to-HEOS bridge."""
    # Configure stream format globals based on --mp3
    global STREAM_PATH, STREAM_CONTENT_TYPE, STREAM_SEND_WAV_HEADER
    if args.mp3:
        STREAM_PATH = STREAM_PATH_MP3
        STREAM_CONTENT_TYPE = "audio/mpeg"
        STREAM_SEND_WAV_HEADER = False

    # Resolve target -- CLI pid or UPnP control URL
    control_url: str | None = None
    pid: int | None = None
    if args.use_upnp:
        control_url = _resolve_upnp_control_url(args.heos_ip, args.heos_port)
        print(f"UPnP AVTransport control URL: {control_url}", file=sys.stderr)
    else:
        pid = _resolve_pid(args.heos_ip, args.pid)

    # Spin up audio fanout BEFORE telling the speaker to start (avoid a race
    # where the speaker connects before our HTTP server is ready).
    global FANOUT
    FANOUT = StdinFanout()
    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), WavStreamHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    my_ip = local_ip_toward(args.heos_ip)
    stream_url = f"http://{my_ip}:{args.http_port}{STREAM_PATH}"
    print(f"Serving live audio at {stream_url}", file=sys.stderr)

    # Tell the speaker to start
    meta_bridge: MetadataBridge | None = None
    if args.use_upnp:
        heos_play_url(control_url, stream_url)
    else:
        # IMPORTANT: do NOT pass station/artist/album/song here. HEOS appends
        # whatever metadata fields we send back onto the stream URL when it
        # fetches -- worse, it joins them with '&' (no '?'), producing requests
        # like "GET /stream.mp3&station=AirPlay" that 404 against any sane HTTP
        # server. We rely on Icy metadata (delivered inline in the audio stream)
        # for title display instead.
        heos_play_stream(args.heos_ip, pid, stream_url)
        # Hook up metadata pipe (volume + optional track metadata forwarding)
        if args.metadata_pipe:
            heos_client = HeosClient(args.heos_ip)
            meta_bridge = MetadataBridge(
                pipe_path=args.metadata_pipe,
                heos=heos_client,
                pid=pid,
                heos_ip=args.heos_ip,
                stream_url=stream_url,
                use_linear=(args.volume_curve == "linear"),
                forward_track_metadata=args.forward_track_metadata,
            )
            meta_bridge.start()
            print(f"Metadata bridge active "
                  f"(volume_curve={args.volume_curve}, "
                  f"forward_tracks={args.forward_track_metadata})",
                  file=sys.stderr)
        else:
            print("Metadata bridge disabled (--metadata-pipe was empty)", file=sys.stderr)
    print("Streaming until interrupted…", file=sys.stderr)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if FANOUT is not None:
            FANOUT.stop()
        if meta_bridge is not None:
            meta_bridge.stop()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="AirPlay (shairport-sync stdout) -> HEOS bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s discover
  %(prog)s probe http://10.6.0.100/device.xml
  %(prog)s list-players --heos-ip 10.6.0.100
  shairport-sync -o stdout -M | %(prog)s bridge --heos-ip 10.6.0.100
""",
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # discover
    p_disc = sub.add_parser("discover",
                            help="scan the LAN for UPnP devices and tag HEOS-looking ones")
    p_disc.set_defaults(func=cmd_discover)

    # probe
    p_probe = sub.add_parser("probe",
                             help="fetch a UPnP device description URL and dump its services")
    p_probe.add_argument("url", help="device description URL (from `discover` output)")
    p_probe.set_defaults(func=cmd_probe)

    # list-players
    p_lp = sub.add_parser("list-players",
                          help="connect to a HEOS CLI port and list all players on the account")
    p_lp.add_argument("--heos-ip", required=True, help="IP of any HEOS device on the account")
    p_lp.set_defaults(func=cmd_list_players)

    # bridge (the main one)
    p_br = sub.add_parser("bridge",
                          help="run the AirPlay-to-HEOS bridge (read PCM from stdin)",
                          formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p_br.add_argument("--heos-ip", required=True, help="HEOS speaker IP")
    p_br.add_argument("--pid", type=int,
                      help="HEOS player ID (default: auto-detected by --heos-ip)")
    p_br.add_argument("--http-port", type=int, default=HTTP_PORT,
                      help="local port on which to serve the live audio stream")
    p_br.add_argument("--mp3", action="store_true",
                      help="serve audio as MP3 (assumes stdin already MP3-encoded by e.g. lame)")
    p_br.add_argument("--metadata-pipe", default="/tmp/shairport-sync-metadata",
                      help="path to shairport-sync's metadata pipe (empty to disable)")
    p_br.add_argument("--volume-curve", choices=("perceptual", "linear"), default="perceptual",
                      help="AirPlay->HEOS volume curve")
    p_br.add_argument("--forward-track-metadata", action="store_true",
                      help="LEGACY: re-issue play_stream on each track change to update the "
                           "HEOS app's 'now playing' card. Causes ~1s audio gap between tracks. "
                           "Usually unnecessary now: track metadata is delivered inline via "
                           "Shoutcast/Icy metadata, which HEOS reads natively without gaps.")
    p_br.add_argument("--use-upnp", action="store_true",
                      help="use UPnP AVTransport instead of HEOS CLI (for non-HEOS renderers)")
    p_br.add_argument("--heos-port", type=int, default=60006,
                      help="UPnP description port (only with --use-upnp)")
    p_br.set_defaults(func=cmd_bridge)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
