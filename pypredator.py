#!/usr/bin/env python3
"""
pyPredator — Linux replacement for Acer PredatorSense on Predator PH18-73.

HANDLES THREE LIGHTING SYSTEMS:

  1. Main keyboard matrix (0x12 envelope, 128 pixels)     — pyusb, direct
  2. WASD / magic-key aux matrix (0x13 envelope)          — pyusb, direct
  3. Infinity ring + cover logo + underglow bar           — shells out to
     linuxbest/acer_PH18-73_rgb CLI which is already tested working.

WHY CONNECT() DETACHES EVERY INTERFACE:
On cold boot the kernel's usbhid driver auto-binds to every HID interface
the device exposes (0, 1, 2, 3). Our ctrl_transfer() targets iface 3 for
SET_REPORT Feature writes, and our bulk writes target the endpoint on
iface 1. If usbhid still owns either one, the claim_interface() call or
the control transfer itself returns EBUSY ("resource busy"). We detach
every interface's kernel driver and claim the two we actually use.
Iface 0 (the typing keyboard, bInterfaceProtocol=1) is left alone by the
companion udev rule so typing keeps working while we're streaming RGB.

WHY THE INIT SEQUENCE MATTERS:
On cold boot the EC runs its own rainbow animation. The init packets
(especially 14 00 01 ... ea) tell the EC to hand control to the host.
Without them, 0x13 (aux / WASD) frames are silently dropped even though
the USB write returns success.

WHY THE DAEMON FORK ON QUIT:
The EC runs a fallback animation if the host stops streaming for ~1-2s.
Closing the GUI therefore reverts the keyboard to rainbow. To keep the
last state visible until next reboot, on quit we save state to
~/.pypredator/state.json, fork a detached child that re-reads that
state and streams it forever. Relaunching the GUI kills any running
daemon first so USB claims don't collide.

PROTOCOL (verified from PredatorSense USB captures):
    Per-frame sequence at ~30 Hz:
        CTRL  13 00 00 08 00 00 00 e4   (aux/WASD begin)
        BULK  1 × 64 B                  (16 aux pixels, 00 R G B each)
        CTRL  12 00 00 08 00 00 00 e5   (main begin)
        BULK  8 × 64 B                  (128 main pixels, 00 R G B each)
        CTRL  08 02 b3 05 32 08 09 fa   (main commit)
        CTRL  08 02 4f 05 32 08 01 66   (aux commit)

SETUP (Ubuntu):
    1. Install pyusb:
           pip install pyusb
       or: sudo apt install python3-usb libusb-1.0-0

    2. Install udev rule (so you don't need sudo every time):
           python3 pypredator.py --setup

    3. (Optional) For infinity ring, cover logo, and underglow bar control,
       clone the linuxbest CLI next to this script or into your home dir:
           git clone https://github.com/linuxbest/acer_PH18-73_rgb.git ~/acer_PH18-73_rgb
       Without it, keyboard RGB still works — only the ring/logo/bar buttons
       will be greyed out.

    4. Run:
           python3 pypredator.py
"""

import os
import sys
import json
import time
import math
import atexit
import signal
import argparse
import threading
import subprocess
import colorsys
from dataclasses import dataclass, asdict
from typing import Tuple, List, Optional

try:
    import usb.core
    import usb.util
    HAVE_USB = True
except ImportError:
    HAVE_USB = False

import tkinter as tk
from tkinter import ttk, colorchooser, messagebox


# =============================================================================
# Config
# =============================================================================

VID, PID       = 0x05AF, 0x867B
KB_IFACE       = 1        # owns EP 0x04 OUT (bulk pixel stream)
EP_OUT         = 0x04
CTRL_IFACE     = 3        # target for class-specific SET_REPORT Feature

NUM_PIXELS     = 128
PACKETS_PER_FR = 8
FRAME_HZ       = 30

_RUN_DIR       = os.path.join(os.path.expanduser("~"), ".pypredator")
os.makedirs(_RUN_DIR, exist_ok=True)
STATE_FILE     = os.path.join(_RUN_DIR, "state.json")
DAEMON_TAG     = "--daemon"          # marker used to find/kill the daemon

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LINUXBEST_CANDIDATES = [
    # Check next to this script first (submodule lives here)
    os.path.join(_SCRIPT_DIR, "acer_PH18-73_rgb"),
    os.path.expanduser("~/acer_PH18-73_rgb"),
    "/opt/acer_PH18-73_rgb",
    os.path.expanduser("~/src/acer_PH18-73_rgb"),
    os.path.expanduser("~/code/acer_PH18-73_rgb"),
]

UDEV_RULE_PATH = "/etc/udev/rules.d/99-predator-rgb.rules"
UDEV_RULE_CONTENT = """\
# pyPredator — allow non-root access to Predator PH18-73 RGB keyboard
SUBSYSTEM=="usb", ATTRS{idVendor}=="05af", ATTRS{idProduct}=="867b", \
    MODE="0666", TAG+="uaccess"
ACTION=="add", SUBSYSTEM=="usb", DRIVER=="usbhid", \
    ATTRS{idVendor}=="05af", ATTRS{idProduct}=="867b", \
    ATTR{bInterfaceNumber}=="01", \
    RUN+="/bin/sh -c 'echo -n %k > /sys/bus/usb/drivers/usbhid/unbind'"
ACTION=="add", SUBSYSTEM=="usb", DRIVER=="usbhid", \
    ATTRS{idVendor}=="05af", ATTRS{idProduct}=="867b", \
    ATTR{bInterfaceNumber}=="03", \
    RUN+="/bin/sh -c 'echo -n %k > /sys/bus/usb/drivers/usbhid/unbind'"
"""


# =============================================================================
# Protocol primitives
# =============================================================================

def cksum(b: bytes) -> int:
    return (0xff - (sum(b[:7]) & 0xff)) & 0xff


def _mk(prefix7_hex: str) -> bytes:
    pre = bytes.fromhex(prefix7_hex)
    assert len(pre) == 7
    return pre + bytes([cksum(pre + b'\x00')])


FRAME_BEGIN_MAIN = _mk('12000008000000')
FRAME_BEGIN_AUX  = _mk('13000008000000')
FRAME_COMMIT     = _mk('0802b305320809')
AUX_COMMIT       = _mk('08024f05320801')

INIT_PACKETS = [
    _mk('88000000000000'),
    _mk('b1000000000000'),
    _mk('14000100000000'),
    _mk('08020000000000'),
]

assert FRAME_BEGIN_MAIN.hex() == '12000008000000e5'
assert FRAME_BEGIN_AUX.hex()  == '13000008000000e4'
assert FRAME_COMMIT.hex()     == '0802b305320809fa'
assert AUX_COMMIT.hex()       == '08024f0532080166'
assert INIT_PACKETS[0].hex() == '8800000000000077'
assert INIT_PACKETS[1].hex() == 'b10000000000004e'
assert INIT_PACKETS[2].hex() == '14000100000000ea'
assert INIT_PACKETS[3].hex() == '08020000000000f5'


PATTERN_MAIN_THEN_AUX   = "main → aux  (two envelopes)"
PATTERN_AUX_THEN_MAIN   = "aux → main  (two envelopes)"
PATTERN_INTERLEAVED     = "main+aux  (single commit)"
PATTERN_INTERLEAVED_REV = "aux+main  (single commit)"
PATTERN_MAIN_ONLY       = "main only (no aux)"
PATTERN_AUX_ONLY        = "aux only  (no main)"

ALL_PATTERNS = [
    PATTERN_MAIN_THEN_AUX,
    PATTERN_AUX_THEN_MAIN,
    PATTERN_INTERLEAVED,
    PATTERN_INTERLEAVED_REV,
    PATTERN_MAIN_ONLY,
    PATTERN_AUX_ONLY,
]

NUM_AUX_PIXELS = 16   # WASD zone: 1 bulk packet = 64 bytes = 16 × (0,R,G,B)


# =============================================================================
# Keyboard USB layer
# =============================================================================

class PredatorKeyboard:
    MAX_CONSEC_ERRS = 16

    def __init__(self):
        self.dev = None
        self.connected = False
        self.err: str = ""
        self.main_ok = 0
        self.main_err = 0
        self.aux_ok = 0
        self.aux_err = 0
        self._consec_errs = 0
        self.init_sent = False
        # Safety net: release USB interfaces on interpreter exit so the
        # typing keyboard is never left in a detached state.
        atexit.register(self._atexit_cleanup)

    def _atexit_cleanup(self):
        try:
            self.disconnect()
        except Exception:
            pass

    # ---- helpers ------------------------------------------------------------

    def _detach_all(self):
        """Detach kernel driver ONLY from the interfaces we claim.

        Interface 0 is the typing keyboard (bInterfaceProtocol=1). Detaching
        usbhid from it kills typing until the device re-enumerates — which
        is exactly the 'keyboard unresponsive while pypredator runs' symptom.
        The udev rule handles unbinding usbhid from 1/3 at enumerate time;
        anything usbhid still owns on those two we clean up here, and we
        never touch iface 0 or iface 2."""
        for ifnum in (KB_IFACE, CTRL_IFACE):
            try:
                if self.dev.is_kernel_driver_active(ifnum):
                    self.dev.detach_kernel_driver(ifnum)
            except (usb.core.USBError, NotImplementedError):
                pass

    def _claim_both(self):
        for ifnum in (KB_IFACE, CTRL_IFACE):
            usb.util.claim_interface(self.dev, ifnum)

    # ---- lifecycle ----------------------------------------------------------

    def connect(self) -> bool:
        if not HAVE_USB:
            self.err = "pyusb not installed (pip install pyusb)"
            return False
        try:
            self.dev = usb.core.find(idVendor=VID, idProduct=PID)
            if self.dev is None:
                self.err = f"device {VID:04x}:{PID:04x} not found"
                return False

            # SAFETY: Do NOT call set_auto_detach_kernel_driver(True).
            # That would auto-detach usbhid from interface 0 (the typing
            # keyboard) when we claim interfaces 1 or 3 — killing keyboard
            # input until the device re-enumerates.

            # Detach kernel driver ONLY from iface 1 & 3, then claim them.
            self._detach_all()
            try:
                self._claim_both()
            except usb.core.USBError as e:
                if getattr(e, "errno", None) == 16:
                    # EBUSY — wait for the udev rule or previous process to
                    # release, then retry.  Avoid dev.reset() which would
                    # re-enumerate the entire device and kill typing.
                    time.sleep(0.8)
                    self._detach_all()
                    self._claim_both()
                else:
                    raise

            self.connected = True
            self.err = ""
            self._consec_errs = 0

            # Host-takeover handshake. Kills the EC's default rainbow.
            self.send_init()
            return True
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            return False

    def disconnect(self):
        if self.dev is not None and self.connected:
            for ifnum in (KB_IFACE, CTRL_IFACE):
                try:
                    usb.util.release_interface(self.dev, ifnum)
                except Exception:
                    pass
                # Re-attach kernel driver so usbhid can manage the
                # interface again (prevents "stuck" detached state).
                try:
                    self.dev.attach_kernel_driver(ifnum)
                except Exception:
                    pass
        self.connected = False

    # ---- protocol -----------------------------------------------------------

    def _send_ctrl(self, payload8: bytes):
        self.dev.ctrl_transfer(0x21, 0x09, (3 << 8) | 0, CTRL_IFACE, payload8)

    def _write_bulk(self, data: bytes):
        n = self.dev.write(EP_OUT, data, timeout=250)
        if n != len(data):
            raise IOError(f"short bulk: {n}/{len(data)}")

    @staticmethod
    def _build_packets(pixels: List[Tuple[int, int, int]]) -> List[bytes]:
        flat = bytearray()
        for r, g, b in pixels:
            flat += bytes((0, r & 0xff, g & 0xff, b & 0xff))
        if len(flat) < 512:
            flat += bytes(512 - len(flat))
        flat = flat[:512]
        return [bytes(flat[i:i + 64]) for i in range(0, 512, 64)]

    def send_init(self) -> bool:
        """Fires the 'host-mode enable' packets. Must run before 0x13 frames
        will actually light WASD on a fresh power cycle."""
        if not self.connected:
            return False
        try:
            for p in INIT_PACKETS:
                self._send_ctrl(p)
                time.sleep(0.008)
            self.init_sent = True
            return True
        except Exception as e:
            self.err = f"init: {type(e).__name__}: {e}"
            return False

    def _env_simple(self, begin: bytes, pixels) -> bool:
        if not self.connected:
            return False
        try:
            self._send_ctrl(begin)
            for pk in self._build_packets(pixels):
                self._write_bulk(pk)
            self._send_ctrl(FRAME_COMMIT)
            self._consec_errs = 0
            return True
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            self._consec_errs += 1
            if self._consec_errs >= self.MAX_CONSEC_ERRS:
                self.connected = False
            return False

    def _env_double(self, begin1: bytes, pixels1, begin2: bytes, pixels2) -> bool:
        if not self.connected:
            return False
        try:
            self._send_ctrl(begin1)
            for pk in self._build_packets(pixels1):
                self._write_bulk(pk)
            self._send_ctrl(begin2)
            for pk in self._build_packets(pixels2):
                self._write_bulk(pk)
            self._send_ctrl(FRAME_COMMIT)
            self._consec_errs = 0
            return True
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            self._consec_errs += 1
            if self._consec_errs >= self.MAX_CONSEC_ERRS:
                self.connected = False
            return False

    def push_pattern(self, pattern: str, main_px, aux_px) -> Tuple[bool, bool]:
        m_ok = a_ok = True
        if pattern == PATTERN_MAIN_THEN_AUX:
            m_ok = self._env_simple(FRAME_BEGIN_MAIN, main_px)
            a_ok = self._env_simple(FRAME_BEGIN_AUX,  aux_px)
        elif pattern == PATTERN_AUX_THEN_MAIN:
            a_ok = self._env_simple(FRAME_BEGIN_AUX,  aux_px)
            m_ok = self._env_simple(FRAME_BEGIN_MAIN, main_px)
        elif pattern == PATTERN_INTERLEAVED:
            ok = self._env_double(FRAME_BEGIN_MAIN, main_px, FRAME_BEGIN_AUX, aux_px)
            m_ok = a_ok = ok
        elif pattern == PATTERN_INTERLEAVED_REV:
            ok = self._env_double(FRAME_BEGIN_AUX, aux_px, FRAME_BEGIN_MAIN, main_px)
            m_ok = a_ok = ok
        elif pattern == PATTERN_MAIN_ONLY:
            m_ok = self._env_simple(FRAME_BEGIN_MAIN, main_px)
        elif pattern == PATTERN_AUX_ONLY:
            a_ok = self._env_simple(FRAME_BEGIN_AUX, aux_px)

        if m_ok: self.main_ok += 1
        else:    self.main_err += 1
        if a_ok: self.aux_ok += 1
        else:    self.aux_err += 1
        return m_ok, a_ok

    @staticmethod
    def _build_aux_packet(pixels: List[Tuple[int, int, int]]) -> bytes:
        """Build a single 64-byte bulk packet for the 16-pixel WASD zone."""
        flat = bytearray()
        for r, g, b in pixels[:NUM_AUX_PIXELS]:
            flat += bytes((0, r & 0xff, g & 0xff, b & 0xff))
        if len(flat) < 64:
            flat += bytes(64 - len(flat))
        return bytes(flat[:64])

    def push_frame(self, main_px, aux_px) -> Tuple[bool, bool]:
        """Send one frame matching the PredatorSense capture protocol:
            0x13 ctrl  →  1 bulk (16 aux pixels)  →
            0x12 ctrl  →  8 bulk (128 main pixels) →
            main commit  →  aux commit
        """
        if not self.connected:
            return False, False
        m_ok = a_ok = False
        try:
            # Aux: 0x13 begin + 1 bulk packet (16 pixels)
            self._send_ctrl(FRAME_BEGIN_AUX)
            self._write_bulk(self._build_aux_packet(aux_px))
            # Main: 0x12 begin + 8 bulk packets (128 pixels)
            self._send_ctrl(FRAME_BEGIN_MAIN)
            for pk in self._build_packets(main_px):
                self._write_bulk(pk)
            # Commits: main first, then aux
            self._send_ctrl(FRAME_COMMIT)
            m_ok = True
            self._send_ctrl(AUX_COMMIT)
            a_ok = True
            self._consec_errs = 0
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            self._consec_errs += 1
            if self._consec_errs >= self.MAX_CONSEC_ERRS:
                self.connected = False
        if m_ok: self.main_ok += 1
        else:    self.main_err += 1
        if a_ok: self.aux_ok += 1
        else:    self.aux_err += 1
        return m_ok, a_ok


# =============================================================================
# System lighting via linuxbest CLI
# =============================================================================

class SystemLights:
    def __init__(self):
        self.repo: Optional[str] = None
        self.cli: Optional[str] = None
        self.err: str = ""
        self._probe()

    def _probe(self):
        for cand in LINUXBEST_CANDIDATES:
            cli = os.path.join(cand, "py", "acer_rgb_led_cli.py")
            if os.path.isfile(cli):
                self.repo = cand
                self.cli = cli
                return
        self.err = "linuxbest repo not found"

    @property
    def available(self) -> bool:
        return self.cli is not None

    def _run(self, zone: str, r: int, g: int, b: int) -> bool:
        if not self.available:
            return False
        hexcol = f"{r:02x}{g:02x}{b:02x}"
        argv = ["python3", self.cli, "apply", zone, "static", "--color", hexcol]
        if os.geteuid() != 0:
            # Try pkexec (graphical prompt) first, fall back to sudo.
            # sudo -n (non-interactive) fails silently without a terminal.
            if os.path.isfile("/usr/bin/pkexec"):
                argv = ["pkexec"] + argv
            else:
                argv = ["sudo"] + argv
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=8)
            if res.returncode != 0:
                self.err = (res.stderr or res.stdout or "nonzero exit").strip()[:160]
                return False
            self.err = ""
            return True
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            return False

    def set_ring(self, r, g, b):  return self._run("rear", r, g, b)
    def set_logo(self, r, g, b):  return self._run("logo", r, g, b)
    def set_bar (self, r, g, b):  return self._run("bar",  r, g, b)


# =============================================================================
# Effect engine
# =============================================================================

@dataclass
class EffectState:
    mode: str = "static"
    main_color:  Tuple[int, int, int] = (255, 0, 0)
    main_color2: Tuple[int, int, int] = (0, 0, 255)
    aux_color:   Tuple[int, int, int] = (255, 0, 0)
    brightness: float = 1.0
    speed:      float = 1.0
    sync_aux:   bool  = True
    pattern:    str   = PATTERN_MAIN_THEN_AUX


def _scale(c, b):
    return (int(c[0] * b), int(c[1] * b), int(c[2] * b))


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, max(0.0, min(1.0, v)))
    return (int(r * 255), int(g * 255), int(b * 255))


def render(mode, t, st, base_color, base_color2):
    b = st.brightness; sp = st.speed; N = NUM_PIXELS
    if mode == "static":
        return [_scale(base_color, b)] * N
    if mode == "breathing":
        phase = (math.sin(t * sp * 2.0) + 1.0) * 0.5
        return [_scale(base_color, b * phase)] * N
    if mode == "wave":
        return [_hsv((i / N + t * sp * 0.25) % 1.0, 1.0, b) for i in range(N)]
    if mode == "rainbow":
        h = (t * sp * 0.1) % 1.0
        return [_hsv(h, 1.0, b)] * N
    if mode == "gradient":
        out = []
        for i in range(N):
            f = i / (N - 1)
            out.append((
                int((base_color[0] * (1-f) + base_color2[0] * f) * b),
                int((base_color[1] * (1-f) + base_color2[1] * f) * b),
                int((base_color[2] * (1-f) + base_color2[2] * f) * b),
            ))
        return out
    if mode == "strobe":
        on = math.sin(t * sp * 6.0) > 0.0
        return [_scale(base_color, b) if on else (0, 0, 0)] * N
    if mode == "fire":
        out = []
        for i in range(N):
            flick = 0.55 + 0.45 * abs(math.sin(t * sp * 3.0 + i * 0.31))
            out.append((int(255 * b * flick), int(70 * b * flick * 0.6), 0))
        return out
    if mode == "pulse_scan":
        out = []
        head = (t * sp * 0.5) % 1.0
        for i in range(N):
            d = abs((i / N) - head); d = min(d, 1.0 - d)
            out.append(_scale(base_color, b * max(0.0, 1.0 - d * 8.0)))
        return out
    return [(0, 0, 0)] * N


class EffectEngine(threading.Thread):
    def __init__(self, kb: PredatorKeyboard, state: EffectState, lock: threading.Lock):
        super().__init__(daemon=True)
        self.kb = kb
        self.state = state
        self.lock = lock
        self.running = True
        self.t_start = time.time()
        self.last_frame: List[Tuple[int,int,int]] = [(0,0,0)] * NUM_PIXELS
        self.last_aux_frame: List[Tuple[int,int,int]] = [(0,0,0)] * NUM_AUX_PIXELS

    def stop(self):
        self.running = False

    def run(self):
        period = 1.0 / FRAME_HZ
        while self.running:
            t0 = time.time()
            t = t0 - self.t_start

            with self.lock:
                st = EffectState(**self.state.__dict__)

            main_px = render(st.mode, t, st, st.main_color, st.main_color2)
            aux_px = render(st.mode, t, st, st.aux_color, st.main_color2)[:NUM_AUX_PIXELS]

            try:
                self.kb.push_frame(main_px, aux_px)
                self.last_frame = main_px
                self.last_aux_frame = aux_px
            except Exception:
                pass

            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)


# =============================================================================
# State persistence (for daemon handoff)
# =============================================================================

def save_state(st: EffectState):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "mode": st.mode,
                "main_color":  list(st.main_color),
                "main_color2": list(st.main_color2),
                "aux_color":   list(st.aux_color),
                "brightness":  st.brightness,
                "speed":       st.speed,
            }, f)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"warning: could not save state: {e}", file=sys.stderr)


def load_state() -> EffectState:
    st = EffectState()
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        st.mode        = d.get("mode", st.mode)
        st.main_color  = tuple(d.get("main_color",  list(st.main_color)))
        st.main_color2 = tuple(d.get("main_color2", list(st.main_color2)))
        st.aux_color   = tuple(d.get("aux_color",   list(st.aux_color)))
        st.brightness  = float(d.get("brightness", st.brightness))
        st.speed       = float(d.get("speed",      st.speed))
        st.sync_aux    = bool(d.get("sync_aux",    st.sync_aux))
        st.pattern     = d.get("pattern", st.pattern)
    except Exception:
        pass
    return st


def kill_running_daemon():
    """Kill any previously-forked pypredator daemon so USB claims don't collide.
    Matches on the script filename + DAEMON_TAG to catch daemons launched from
    any path (e.g. ~/Downloads vs ~/pypredator)."""
    try:
        subprocess.run(["pkill", "-f", f"pypredator.*{DAEMON_TAG}"],
                       capture_output=True, timeout=2)
        # Give USB time to actually release.
        time.sleep(0.4)
    except Exception:
        pass


DAEMON_LOG = os.path.join(_RUN_DIR, "daemon.log")

def spawn_daemon():
    """Launch a detached daemon that keeps streaming the saved state.

    Uses subprocess.Popen with start_new_session=True rather than os.fork()
    because on Python 3.12+ fork() in a multi-threaded process (Tk counts)
    emits a DeprecationWarning — and future Pythons may make it outright
    unsafe. Popen creates a new session and process group, fully detaching
    the child from the GUI's lifecycle."""
    try:
        log = open(DAEMON_LOG, "w")
        subprocess.Popen(
            [sys.executable, "-u", os.path.abspath(__file__), DAEMON_TAG],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    except Exception as e:
        print(f"warning: failed to spawn daemon: {e}")


# =============================================================================
# HID report descriptor dump (for WASD / aux matrix investigation)
# =============================================================================

def dump_hid_descriptors():
    """Dump HID report descriptors for every interface, via pyusb GET_DESCRIPTOR.

    We can't read these from /sys/kernel/debug/hid/ anymore because our udev
    rule unbinds usbhid from interfaces 1 and 3, so they never get a debugfs
    entry. Going direct to the device over USB sidesteps that entirely.

    For interfaces still bound to usbhid (0 = typing keyboard, probably 2 =
    consumer control), we briefly detach, read, and re-attach."""
    if not HAVE_USB:
        print("pyusb not installed (pip install pyusb)")
        return
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print(f"device {VID:04x}:{PID:04x} not found")
        return

    cfg = dev.get_active_configuration()
    print(f"# Predator PH18-73 RGB {VID:04x}:{PID:04x} — HID report descriptors")
    print(f"# {cfg.bNumInterfaces} interfaces on active config\n")

    for intf in cfg:
        ifnum = intf.bInterfaceNumber
        print(f"## interface {ifnum}  "
              f"(class {intf.bInterfaceClass}, "
              f"subclass {intf.bInterfaceSubClass}, "
              f"protocol {intf.bInterfaceProtocol})")

        # Endpoints summary — useful context.
        for ep in intf:
            direction = "IN" if (ep.bEndpointAddress & 0x80) else "OUT"
            ep_type = {0: "CTRL", 1: "ISO", 2: "BULK", 3: "INT"}.get(
                ep.bmAttributes & 0x03, "?")
            print(f"   ep 0x{ep.bEndpointAddress:02x} {direction} {ep_type} "
                  f"wMaxPacketSize={ep.wMaxPacketSize}")

        # SAFETY: never detach interface 0 — that is the typing keyboard.
        if ifnum == 0:
            print("   (skipping detach — iface 0 is the typing keyboard)")
            print()
            continue

        was_attached = False
        try:
            if dev.is_kernel_driver_active(ifnum):
                dev.detach_kernel_driver(ifnum)
                was_attached = True
        except (usb.core.USBError, NotImplementedError):
            pass

        try:
            # bmRequestType=0x81 (IN, standard, interface recipient)
            # bRequest=0x06 (GET_DESCRIPTOR)
            # wValue=(0x22 << 8) (Report descriptor type, index 0)
            desc = dev.ctrl_transfer(0x81, 0x06, 0x2200, ifnum, 4096, timeout=1000)
            desc = bytes(desc)
            print(f"   report descriptor: {len(desc)} bytes")
            for i in range(0, len(desc), 16):
                chunk = desc[i:i+16]
                hexpart = ' '.join(f'{b:02x}' for b in chunk)
                print(f"      {i:04x}  {hexpart}")
        except Exception as e:
            print(f"   report descriptor: FAILED — {type(e).__name__}: {e}")
        finally:
            if was_attached:
                try:
                    dev.attach_kernel_driver(ifnum)
                except Exception as e:
                    print(f"   !! failed to re-attach usbhid on iface {ifnum}: {e}")
                    print(f"   !! run:  sudo modprobe -r usbhid && sudo modprobe usbhid")
        print()




# =============================================================================
# Daemon entry point (headless frame pusher)
# =============================================================================

def run_daemon():
    """Headless: load saved state, connect, stream forever until killed."""
    print(f"daemon starting, pid={os.getpid()}")
    kb = PredatorKeyboard()
    # Retry connect — parent may not have fully released USB yet.
    for attempt in range(20):
        if kb.connect():
            print(f"daemon connected on attempt {attempt + 1}")
            break
        print(f"daemon connect attempt {attempt + 1} failed: {kb.err}")
        time.sleep(0.5)
    else:
        print("daemon giving up after 20 attempts")
        sys.exit(1)

    state = load_state()
    print(f"daemon state: mode={state.mode} main={state.main_color} aux={state.aux_color}")
    lock = threading.Lock()
    engine = EffectEngine(kb, state, lock)
    engine.start()

    stop_evt = threading.Event()
    def _sig(_s, _f):
        stop_evt.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT,  _sig)

    try:
        while not stop_evt.is_set() and engine.is_alive():
            stop_evt.wait(0.5)
    finally:
        engine.stop()
        engine.join(timeout=1.0)
        kb.disconnect()


# =============================================================================
# GUI
# =============================================================================

BG       = "#0a0d12"
BG2      = "#12161d"
BG3      = "#1a1f28"
FG       = "#d0e4ff"
ACCENT   = "#00ff9c"
ACCENT_D = "#007a4a"
DANGER   = "#ff4d6d"
WARN     = "#ffcc00"
MUTED    = "#6b7280"
FONT     = ("JetBrains Mono", 10)
FONT_B   = ("JetBrains Mono", 11, "bold")
FONT_H   = ("JetBrains Mono", 18, "bold")

PRESETS = [
    ("RED",     (255,   0,   0)),
    ("GREEN",   (  0, 255,   0)),
    ("BLUE",    (  0,   0, 255)),
    ("CYAN",    (  0, 255, 255)),
    ("MAGENTA", (255,   0, 255)),
    ("YELLOW",  (255, 255,   0)),
    ("WHITE",   (255, 255, 255)),
    ("OFF",     (  0,   0,   0)),
]

MODES = [
    ("Static",     "static"),
    ("Breathing",  "breathing"),
    ("Wave",       "wave"),
    ("Rainbow",    "rainbow"),
    ("Gradient",   "gradient"),
    ("Strobe",     "strobe"),
    ("Fire",       "fire"),
    ("Pulse Scan", "pulse_scan"),
]


class PredatorSenseGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("pyPredator :: Linux PredatorSense")
        root.geometry("820x680")
        root.configure(bg=BG)
        root.minsize(800, 600)

        self.kb = PredatorKeyboard()
        self.sys = SystemLights()
        # Restore prior state so relaunching the GUI picks up where you left off.
        self.state = load_state()
        self.lock = threading.Lock()
        self.engine: Optional[EffectEngine] = None

        self._setup_style()
        self._build_ui()
        self._sync_ui_from_state()
        self._connect_device()
        self._tick_preview()

    def _setup_style(self):
        st = ttk.Style()
        try: st.theme_use("clam")
        except Exception: pass
        st.configure("TFrame",         background=BG)
        st.configure("Card.TFrame",    background=BG2)
        st.configure("TLabel",         background=BG,  foreground=FG,     font=FONT)
        st.configure("Card.TLabel",    background=BG2, foreground=FG,     font=FONT)
        st.configure("Header.TLabel",  background=BG,  foreground=ACCENT, font=FONT_H)
        st.configure("Section.TLabel", background=BG2, foreground=ACCENT, font=FONT_B)
        st.configure("Muted.TLabel",   background=BG,  foreground=MUTED,  font=FONT)
        st.configure("TButton",
                     background=BG3, foreground=FG, font=FONT,
                     borderwidth=0, focusthickness=0, padding=6)
        st.map("TButton",
               background=[("active", ACCENT_D)],
               foreground=[("active", "#ffffff")])
        st.configure("Accent.TButton",
                     background=ACCENT_D, foreground="#ffffff", font=FONT_B, padding=8)
        st.map("Accent.TButton", background=[("active", ACCENT)])
        st.configure("Warn.TButton",
                     background="#553300", foreground="#ffe0a0", font=FONT_B, padding=8)
        st.map("Warn.TButton", background=[("active", WARN)])
        st.configure("TRadiobutton", background=BG2, foreground=FG, font=FONT)
        st.map("TRadiobutton", background=[("active", BG2)])
        st.configure("TCheckbutton", background=BG2, foreground=FG, font=FONT)
        st.map("TCheckbutton", background=[("active", BG2)])
        st.configure("TScale", background=BG2, troughcolor=BG3)
        st.configure("TLabelframe", background=BG2, foreground=ACCENT,
                     bordercolor=BG3, relief="solid")
        st.configure("TLabelframe.Label", background=BG2, foreground=ACCENT, font=FONT_B)
        st.configure("TCombobox", fieldbackground=BG3, background=BG3, foreground=FG)

    # ---- zone card helper -----------------------------------------------------

    def _make_zone_card(self, parent, label, row, col, checked=True):
        """Create a zone card: checkbox + color swatch.  Returns (BooleanVar, Canvas)."""
        fr = ttk.Frame(parent, style="Card.TFrame")
        fr.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
        var = tk.BooleanVar(value=checked)
        ttk.Checkbutton(fr, text=label, variable=var,
                        style="TCheckbutton").pack(anchor="w", padx=8, pady=(8, 4))
        sw = tk.Canvas(fr, width=80, height=32, bg="#ff0000",
                       highlightthickness=1, highlightbackground=BG3)
        sw.pack(padx=8, pady=(0, 8))
        return var, sw

    # ---- build UI -----------------------------------------------------------

    def _build_ui(self):
        hdr = ttk.Frame(self.root)
        hdr.pack(fill="x", padx=20, pady=(18, 6))
        ttk.Label(hdr, text="◤  pyPredator  ◥", style="Header.TLabel").pack(side="left")
        self.status_lbl = ttk.Label(hdr, text="● disconnected",
                                    foreground=DANGER, background=BG, font=FONT_B)
        self.status_lbl.pack(side="right")

        # ---- live preview ---------------------------------------------------
        prev = ttk.LabelFrame(self.root, text="  live preview  ")
        prev.pack(fill="x", padx=20, pady=6)
        self.preview = tk.Canvas(prev, height=36, bg=BG3, highlightthickness=0, bd=0)
        self.preview.pack(fill="x", padx=10, pady=(10, 4))
        self.preview_aux = tk.Canvas(prev, height=18, bg=BG3, highlightthickness=0, bd=0)
        self.preview_aux.pack(fill="x", padx=10, pady=(0, 10))

        # ---- zones ----------------------------------------------------------
        zone_fr = ttk.LabelFrame(self.root, text="  zones  (tick to edit, pick color, untick to lock)  ")
        zone_fr.pack(fill="x", padx=20, pady=6)

        self.zone_main_var,  self.main_sw  = self._make_zone_card(zone_fr, "Keyboard",  0, 0, True)
        self.zone_wasd_var,  self.aux_sw   = self._make_zone_card(zone_fr, "WASD",      0, 1, True)
        self.zone_ring_var,  self.ring_sw  = self._make_zone_card(zone_fr, "Ring",       0, 2, False)
        self.zone_logo_var,  self.logo_sw  = self._make_zone_card(zone_fr, "Logo",       0, 3, False)
        self.zone_bar_var,   self.bar_sw   = self._make_zone_card(zone_fr, "Bar",        0, 4, False)
        for i in range(5):
            zone_fr.columnconfigure(i, weight=1)

        # Gradient secondary color (only for gradient effect)
        self.main2_sw = tk.Canvas(zone_fr, width=0, height=0)  # hidden, kept for state

        # ---- color picker + presets -----------------------------------------
        col_fr = ttk.LabelFrame(self.root, text="  color  (applies to all ticked zones)  ")
        col_fr.pack(fill="x", padx=20, pady=6)

        ttk.Button(col_fr, text="Pick color...", style="Accent.TButton",
                   command=self._pick_color).grid(row=0, column=0, padx=12, pady=10, sticky="w")

        for i, (label, col) in enumerate(PRESETS):
            ttk.Button(col_fr, text=label,
                       command=lambda c=col, lbl=label: self._apply_preset(c, lbl)
                       ).grid(row=0, column=i + 1, padx=4, pady=10, sticky="ew")
            col_fr.columnconfigure(i + 1, weight=1)

        # ---- effect ---------------------------------------------------------
        mode_fr = ttk.LabelFrame(self.root, text="  effect  (keyboard + WASD)  ")
        mode_fr.pack(fill="x", padx=20, pady=6)
        self.mode_var = tk.StringVar(value="static")
        for i, (label, val) in enumerate(MODES):
            ttk.Radiobutton(mode_fr, text=label, value=val, variable=self.mode_var,
                            command=self._on_mode
                            ).grid(row=i // 4, column=i % 4, sticky="w", padx=14, pady=6)

        # ---- parameters -----------------------------------------------------
        sl_fr = ttk.LabelFrame(self.root, text="  parameters  ")
        sl_fr.pack(fill="x", padx=20, pady=6)
        ttk.Label(sl_fr, text="brightness", style="Card.TLabel").grid(row=0, column=0, padx=12, pady=8, sticky="w")
        self.bright_var = tk.DoubleVar(value=1.0)
        ttk.Scale(sl_fr, from_=0.0, to=1.0, variable=self.bright_var,
                  orient="horizontal", length=480,
                  command=lambda _=None: self._on_slider()).grid(row=0, column=1, padx=8, pady=8)
        self.bright_lbl = ttk.Label(sl_fr, text="100%", style="Card.TLabel")
        self.bright_lbl.grid(row=0, column=2, padx=10)

        ttk.Label(sl_fr, text="speed", style="Card.TLabel").grid(row=1, column=0, padx=12, pady=8, sticky="w")
        self.speed_var = tk.DoubleVar(value=1.0)
        ttk.Scale(sl_fr, from_=0.1, to=4.0, variable=self.speed_var,
                  orient="horizontal", length=480,
                  command=lambda _=None: self._on_slider()).grid(row=1, column=1, padx=8, pady=8)
        self.speed_lbl = ttk.Label(sl_fr, text="1.00×", style="Card.TLabel")
        self.speed_lbl.grid(row=1, column=2, padx=10)

        # ---- bottom bar -----------------------------------------------------
        btm = ttk.Frame(self.root)
        btm.pack(fill="x", padx=20, pady=14)
        ttk.Button(btm, text="all off",
                   command=self._all_off).pack(side="left", padx=4)
        ttk.Button(btm, text="reconnect",
                   command=self._connect_device).pack(side="left", padx=4)
        ttk.Button(btm, text="re-init EC",
                   command=self._reinit).pack(side="left", padx=4)
        ttk.Button(btm, text="quit + keep lights",
                   command=self._quit).pack(side="right", padx=4)

    def _sync_ui_from_state(self):
        """After loading saved state, push values into the Tk variables and swatches."""
        self.mode_var.set(self.state.mode)
        self.bright_var.set(self.state.brightness)
        self.speed_var.set(self.state.speed)
        self.bright_lbl.configure(text=f"{int(self.state.brightness*100)}%")
        self.speed_lbl.configure(text=f"{self.state.speed:.2f}×")
        self.main_sw.configure(bg="#%02x%02x%02x" % tuple(self.state.main_color))
        self.aux_sw .configure(bg="#%02x%02x%02x" % tuple(self.state.aux_color))

    def _tick_preview(self):
        w = self.preview.winfo_width() or 760
        self.preview.delete("all")
        self.preview_aux.delete("all")
        if self.engine is None:
            px = [(0,0,0)] * NUM_PIXELS
            ax = [(0,0,0)] * NUM_PIXELS
        else:
            px = self.engine.last_frame
            ax = self.engine.last_aux_frame
        step = w / NUM_PIXELS
        for i, (r, g, b) in enumerate(px):
            self.preview.create_rectangle(i * step, 0, (i + 1) * step, 44,
                                          fill=f"#{r:02x}{g:02x}{b:02x}", outline="")
        for i, (r, g, b) in enumerate(ax[:16]):
            self.preview_aux.create_rectangle(i * (w / 16), 0, (i + 1) * (w / 16), 22,
                                              fill=f"#{r:02x}{g:02x}{b:02x}", outline="")

        if self.kb.connected:
            init_mark = "✓init" if self.kb.init_sent else "✗init"
            txt = (f"● connected  {init_mark}  "
                   f"main {self.kb.main_ok}/{self.kb.main_err}  "
                   f"aux {self.kb.aux_ok}/{self.kb.aux_err}")
            col = ACCENT
            if self.kb.err:
                txt += f"  :: {self.kb.err[:48]}"
                col = WARN
            self.status_lbl.configure(text=txt, foreground=col)
        else:
            self.status_lbl.configure(
                text=f"● {self.kb.err or 'disconnected'}", foreground=DANGER)
        self.root.after(100, self._tick_preview)

    def _on_mode(self):
        with self.lock: self.state.mode = self.mode_var.get()

    def _on_slider(self):
        with self.lock:
            self.state.brightness = float(self.bright_var.get())
            self.state.speed      = float(self.speed_var.get())
        self.bright_lbl.configure(text=f"{int(self.state.brightness*100)}%")
        self.speed_lbl.configure(text=f"{self.state.speed:.2f}×")

    def _apply_color(self, col):
        """Push *col* to every ticked zone, update swatches + state."""
        hexcol = "#%02x%02x%02x" % col
        if self.zone_main_var.get():
            with self.lock: self.state.main_color = col
            self.main_sw.configure(bg=hexcol)
        if self.zone_wasd_var.get():
            with self.lock: self.state.aux_color = col
            self.aux_sw.configure(bg=hexcol)
        # System lights (ring / logo / bar) — fire-and-forget in background
        sys_jobs = []
        if self.zone_ring_var.get():
            self.ring_sw.configure(bg=hexcol)
            sys_jobs.append(lambda r=col[0], g=col[1], b=col[2]: self.sys.set_ring(r, g, b))
        if self.zone_logo_var.get():
            self.logo_sw.configure(bg=hexcol)
            sys_jobs.append(lambda r=col[0], g=col[1], b=col[2]: self.sys.set_logo(r, g, b))
        if self.zone_bar_var.get():
            self.bar_sw.configure(bg=hexcol)
            sys_jobs.append(lambda r=col[0], g=col[1], b=col[2]: self.sys.set_bar(r, g, b))
        if sys_jobs:
            def _run_sys():
                for fn in sys_jobs:
                    fn()
            threading.Thread(target=_run_sys, daemon=True).start()

    def _pick_color(self):
        """Open the color picker and apply the chosen color to all ticked zones."""
        c = colorchooser.askcolor(title="Pick color for ticked zones")
        if c and c[0]:
            self._apply_color(tuple(int(x) for x in c[0]))

    def _apply_preset(self, col, label):
        if label == "OFF":
            with self.lock:
                self.state.mode = "static"
            self.mode_var.set("static")
        self._apply_color(col)

    def _reinit(self):
        ok = self.kb.send_init()
        messagebox.showinfo("re-init",
            f"init handshake {'sent' if ok else 'FAILED'}\n"
            f"last error: {self.kb.err or '(none)'}")


    def _all_off(self):
        """Turn off all zones regardless of checkboxes."""
        off = (0, 0, 0)
        with self.lock:
            self.state.mode = "static"
            self.state.main_color = off
            self.state.aux_color = off
        self.mode_var.set("static")
        self.main_sw.configure(bg="#000000")
        self.aux_sw.configure(bg="#000000")
        self.ring_sw.configure(bg="#000000")
        self.logo_sw.configure(bg="#000000")
        self.bar_sw.configure(bg="#000000")
        if self.sys.available:
            threading.Thread(target=lambda: (
                self.sys.set_logo(0,0,0),
                self.sys.set_ring(0,0,0),
                self.sys.set_bar (0,0,0),
            ), daemon=True).start()

    def _connect_device(self):
        if self.engine is not None:
            self.engine.stop()
            self.engine.join(timeout=2.0)
            self.engine = None
        self.kb.disconnect()
        if self.kb.connect():
            self.engine = EffectEngine(self.kb, self.state, self.lock)
            self.engine.start()

    def _quit(self):
        """Save state, release USB, then fork a detached daemon that keeps
        streaming the saved state so the keyboard doesn't revert to rainbow."""
        # Snapshot current state to disk so the daemon can pick it up.
        with self.lock:
            snapshot = EffectState(**self.state.__dict__)
        save_state(snapshot)

        # Stop the GUI's engine and release USB cleanly so the daemon can claim.
        if self.engine is not None:
            self.engine.stop()
            self.engine.join(timeout=2.0)
            self.engine = None
        self.kb.disconnect()

        # Grace period for Linux to fully release the interfaces before
        # the daemon tries to claim them.
        time.sleep(0.5)

        # Hand off to a detached daemon.
        spawn_daemon()

        self.root.destroy()


# =============================================================================
# Entry point
# =============================================================================

def install_udev_rule():
    """Install the udev rule so pypredator can access the USB device without sudo."""
    if os.path.isfile(UDEV_RULE_PATH):
        print(f"udev rule already exists at {UDEV_RULE_PATH}")
        return True
    print(f"Installing udev rule to {UDEV_RULE_PATH} ...")
    try:
        subprocess.run(
            ["sudo", "tee", UDEV_RULE_PATH],
            input=UDEV_RULE_CONTENT.encode(), capture_output=True, check=True)
        subprocess.run(["sudo", "udevadm", "control", "--reload"], check=True)
        subprocess.run(["sudo", "udevadm", "trigger", "--action=add",
                        "--attr-match=idVendor=05af",
                        "--attr-match=idProduct=867b"], check=True)
        print("udev rule installed. You can now run pypredator without sudo.")
        print("(Unplug and replug the USB keyboard, or reboot, if it doesn't take effect.)")
        return True
    except Exception as e:
        print(f"Failed to install udev rule: {e}")
        return False


def check_deps():
    """Check that pyusb is installed; print a helpful message if not."""
    if HAVE_USB:
        return True
    print("ERROR: pyusb is required but not installed.\n")
    print("Install it with one of:")
    print("    pip install pyusb")
    print("    sudo apt install python3-usb")
    print("")
    print("On Ubuntu you may also need the libusb backend:")
    print("    sudo apt install libusb-1.0-0")
    return False


def main():
    ap = argparse.ArgumentParser(
        description="pyPredator — Linux RGB control for Acer Predator PH18-73",
        epilog="First-time setup:  python3 pypredator.py --setup")
    ap.add_argument(DAEMON_TAG, dest="daemon", action="store_true",
                    help="internal: run as headless frame pusher (not for direct use)")
    ap.add_argument("--stop", action="store_true",
                    help="kill any running pypredator daemon and exit")
    ap.add_argument("--dump-hid", action="store_true",
                    help="dump HID report descriptors for all 4 interfaces and exit")
    ap.add_argument("--setup", action="store_true",
                    help="install udev rule (so sudo isn't needed) and check dependencies")
    args = ap.parse_args()

    if args.setup:
        ok = check_deps()
        if ok:
            print("pyusb ............. OK")
        install_udev_rule()
        return

    if args.dump_hid:
        if not check_deps():
            sys.exit(1)
        kill_running_daemon()
        dump_hid_descriptors()
        return

    if args.stop:
        kill_running_daemon()
        print("stopped any running pypredator daemon")
        return

    if args.daemon:
        run_daemon()
        return

    if not check_deps():
        sys.exit(1)

    # GUI path: kill any previously-spawned daemon so we can claim USB.
    kill_running_daemon()

    root = tk.Tk()
    app = PredatorSenseGUI(root)
    root.protocol("WM_DELETE_WINDOW", app._quit)
    root.mainloop()


if __name__ == "__main__":
    main()
