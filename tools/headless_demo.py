"""Run the pyPredator GUI against a fake keyboard (no USB) and cycle effects.

This is how docs/media was produced: no Predator laptop, no USB access,
just the real GUI and the real render() loop with the USB layer swapped out.

Usage (headless):
    Xvfb :87 -screen 0 820x680x24 &
    DISPLAY=:87 HOME=$(mktemp -d) python3 tools/headless_demo.py pypredator.py \
        wave,fire,pulse_scan,gradient,breathing 1800
    ffmpeg -f x11grab -draw_mouse 0 -framerate 15 -video_size 820x680 -i :87 -t 9 demo.mp4

Arguments: path to pypredator.py, comma-separated modes, dwell per mode in ms.
"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("pypredator", sys.argv[1])
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)


class FakeKeyboard:
    """Stand-in for PredatorKeyboard: accepts every frame, touches no USB."""

    def __init__(self):
        self.connected = False
        self.err = ""
        self.init_sent = False
        self.main_ok = self.main_err = self.aux_ok = self.aux_err = 0

    def connect(self):
        self.connected = True
        self.init_sent = True
        return True

    def disconnect(self):
        pass

    def send_init(self):
        return True

    def push_frame(self, main_px, aux_px):
        self.main_ok += 1
        self.aux_ok += 1
        return True, True


pp.PredatorKeyboard = FakeKeyboard
# JetBrains Mono is not installed in the capture env; use a font with the GUI glyphs.
pp.FONT = ("DejaVu Sans Mono", 10)
pp.FONT_B = ("DejaVu Sans Mono", 11, "bold")
pp.FONT_H = ("DejaVu Sans Mono", 18, "bold")

root = pp.tk.Tk()
app = pp.PredatorSenseGUI(root)
app.state.main_color = (0, 255, 156)
app.state.aux_color = (255, 0, 80)
app.state.main_color2 = (120, 0, 255)
app._sync_ui_from_state()

modes = sys.argv[2].split(",") if len(sys.argv) > 2 else ["wave"]
dwell_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 2000


def step(i=0):
    if i >= len(modes):
        root.after(dwell_ms, root.destroy)
        return
    app.mode_var.set(modes[i])
    app._on_mode()
    root.after(dwell_ms, step, i + 1)


root.after(500, step)
root.mainloop()
