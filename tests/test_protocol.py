"""Hardware-free tests for the pure parts of pypredator.py.

Covers the 8-byte control packets (checksum), the bulk packet builders and
the shape of every effect frame. No USB device, no display needed.
Run: python3 -m unittest discover -s tests
"""
import importlib.util
import os
import sys
import tempfile
import unittest

# pypredator creates ~/.pypredator at import time; keep that out of the real home.
os.environ["HOME"] = tempfile.mkdtemp(prefix="pypredator-test-")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("pypredator", os.path.join(_ROOT, "pypredator.py"))
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

MODES = [m for _, m in pp.MODES]


class ControlPackets(unittest.TestCase):
    def test_checksum_makes_bytes_sum_to_ff(self):
        # The EC accepts a packet when its 8 bytes sum to 0xff (mod 256).
        for pkt in [pp.FRAME_BEGIN_MAIN, pp.FRAME_BEGIN_AUX, pp.FRAME_COMMIT,
                    pp.AUX_COMMIT, *pp.INIT_PACKETS]:
            self.assertEqual(len(pkt), 8)
            self.assertEqual(sum(pkt) & 0xff, 0xff, pkt.hex())

    def test_known_capture_values(self):
        # Bytes as seen in the PredatorSense USB captures.
        self.assertEqual(pp.FRAME_BEGIN_MAIN.hex(), "12000008000000e5")
        self.assertEqual(pp.FRAME_COMMIT.hex(), "0802b305320809fa")
        self.assertEqual(pp.INIT_PACKETS[2].hex(), "14000100000000ea")

    def test_mk_rejects_wrong_length(self):
        with self.assertRaises(AssertionError):
            pp._mk("1200")


class BulkPackets(unittest.TestCase):
    def test_main_frame_is_8_packets_of_64(self):
        pkts = pp.PredatorKeyboard._build_packets([(1, 2, 3)] * pp.NUM_PIXELS)
        self.assertEqual([len(p) for p in pkts], [64] * 8)
        self.assertEqual(pkts[0][:8], bytes([0, 1, 2, 3, 0, 1, 2, 3]))

    def test_short_main_frame_is_zero_padded(self):
        pkts = pp.PredatorKeyboard._build_packets([(255, 0, 0)])
        self.assertEqual(pkts[0][:4], bytes([0, 255, 0, 0]))
        self.assertEqual(b"".join(pkts)[4:], bytes(508))

    def test_aux_packet_is_16_pixels(self):
        pkt = pp.PredatorKeyboard._build_aux_packet([(9, 8, 7)] * 40)
        self.assertEqual(len(pkt), 64)
        self.assertEqual(pkt[-4:], bytes([0, 9, 8, 7]))


class Effects(unittest.TestCase):
    def test_every_mode_renders_128_valid_pixels(self):
        st = pp.EffectState()
        for mode in MODES:
            for t in (0.0, 0.37, 5.0, 123.4):
                px = pp.render(mode, t, st, (255, 40, 0), (0, 0, 255))
                self.assertEqual(len(px), pp.NUM_PIXELS, mode)
                for p in px:
                    self.assertEqual(len(p), 3)
                    self.assertTrue(all(0 <= c <= 255 for c in p), (mode, p))

    def test_static_respects_brightness(self):
        st = pp.EffectState(brightness=0.5)
        self.assertEqual(pp.render("static", 0, st, (200, 100, 0), (0, 0, 0))[0], (100, 50, 0))

    def test_gradient_endpoints(self):
        st = pp.EffectState()
        px = pp.render("gradient", 0, st, (255, 0, 0), (0, 0, 255))
        self.assertEqual(px[0], (255, 0, 0))
        self.assertEqual(px[-1], (0, 0, 255))

    def test_unknown_mode_is_black(self):
        st = pp.EffectState()
        self.assertEqual(set(pp.render("nope", 0, st, (1, 1, 1), (1, 1, 1))), {(0, 0, 0)})


if __name__ == "__main__":
    unittest.main()
