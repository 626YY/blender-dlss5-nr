"""Standalone test of the NR monitor window: start worker, point it at Blender's window, screenshot the desktop."""
import subprocess, struct, json, sys, time, os, ctypes, ctypes.wintypes as W
import numpy as np
from PIL import Image
MAGIC = b"NRW1"
BPY = os.environ.get("BLENDER_PYTHON", r"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe")
WORKER, OUT = sys.argv[1], sys.argv[2]
ROOT = os.environ.get("DLSS5_NR_ROOT", r"C:\ComfyUI\custom_nodes\ComfyUI-DLSS5-NR")
user32 = ctypes.windll.user32; gdi32 = ctypes.windll.gdi32
VP = ctypes.c_void_p
user32.SetProcessDpiAwarenessContext(VP(-4))
user32.GetDC.restype = VP; user32.GetDC.argtypes = [VP]; user32.ReleaseDC.argtypes = [VP, VP]
user32.GetClientRect.argtypes = [VP, ctypes.POINTER(W.RECT)]; user32.ClientToScreen.argtypes = [VP, ctypes.POINTER(W.POINT)]
gdi32.CreateCompatibleDC.restype = VP; gdi32.CreateCompatibleDC.argtypes = [VP]
gdi32.SelectObject.restype = VP; gdi32.SelectObject.argtypes = [VP, VP]
gdi32.DeleteObject.argtypes = [VP]; gdi32.DeleteDC.argtypes = [VP]
gdi32.BitBlt.argtypes = [VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, VP, ctypes.c_int, ctypes.c_int, W.DWORD]


def send(p, obj, payload=b""):
    if payload:
        obj["bytes"] = len(payload)
    js = json.dumps(obj).encode()
    p.stdin.write(MAGIC + struct.pack("<I", len(js)) + js + payload); p.stdin.flush()


def rexact(f, n):
    b = b""
    while len(b) < n:
        c = f.read(n - len(b))
        if not c:
            return None
        b += c
    return b


def recv(p):
    h = rexact(p.stdout, 8); assert h and h[:4] == MAGIC, h
    (n,) = struct.unpack("<I", h[4:]); obj = json.loads(rexact(p.stdout, n))
    pl = rexact(p.stdout, obj.get("bytes", 0)) if obj.get("bytes") else b""
    return obj, pl


# find Blender window
found = []
proto = ctypes.WINFUNCTYPE(ctypes.c_bool, VP, VP)
def cb(hwnd, lp):
    if user32.IsWindowVisible(hwnd):
        n = user32.GetWindowTextLengthW(hwnd); buf = ctypes.create_unicode_buffer(n + 1); user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(64); user32.GetClassNameW(hwnd, cls, 64)
        if "Blender" in buf.value and "GHOST" in cls.value:
            found.append(hwnd)
    return True
user32.EnumWindows(proto(cb), 0)
hwnd = int(found[0]); print("blender hwnd", hex(hwnd))
user32.SetForegroundWindow.argtypes = [VP]; user32.SetForegroundWindow(hwnd)   # monitor hides itself unless Blender is foreground
rc = W.RECT(); user32.GetClientRect(hwnd, ctypes.byref(rc)); cw, ch = rc.right, rc.bottom
# use the left 3D viewport region approx: x 0..1353, y 40..1096 (client coords) - take a safe sub-rect
args = [a for a in sys.argv[3:] if not a.startswith("--")]
rect = [int(args[0]), int(args[1]), int(args[2]), int(args[3])] if len(args) >= 4 else [60, 60, 1200, 900]
print("client", cw, ch, "rect", rect)

p = subprocess.Popen([BPY, "-u", WORKER, ROOT, "0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(OUT + "_monitor.stderr.txt", "wb"))
print("hello", recv(p)[0])
cfg = dict(hwnd=hwnd, rect=rect, style=2, tone=0.0, structure=2.0, skin=1.5, automask=True, mode="COLOR", strength=1.0, half=False, split=0.5, view="SPLIT",
           dump=OUT + "_frame.npy", headless=("--headless" in sys.argv), bench=("--bench" in sys.argv))
send(p, {"cmd": "monitor", "action": "start", "cfg": cfg}); print("start", recv(p)[0])
for i in range(6):
    time.sleep(0.5)
    send(p, {"cmd": "monitor", "action": "status"}); st, _ = recv(p); print("status %.1fs" % ((i + 1) * 0.5), st)
# inspect the composed frame the monitor dumped (BGRA premultiplied, top-down)
try:
    fr = np.load(OUT + "_frame.npy")
    rgb = fr[..., [2, 1, 0]]; a = fr[..., 3]
    print("dump", fr.shape, "alpha left/right mean:", a[:, :fr.shape[1] // 2].mean(), a[:, fr.shape[1] // 2:].mean())
    Image.fromarray(rgb).save(OUT + "_frame_rgb.png"); Image.fromarray(a).save(OUT + "_frame_alpha.png")
except Exception as e:
    print("dump ERR", e)
# half-res + NR view
send(p, {"cmd": "monitor", "action": "update", "cfg": {"half": True, "view": "NR", "dump": OUT + "_frame_half.npy"}}); print("update", recv(p)[0])
for i in range(6):
    time.sleep(0.5); send(p, {"cmd": "monitor", "action": "status"}); st, _ = recv(p)
    print("half %.1fs fps=%.1f cap=%.0f nr=%.0f present=%.0f frames=%d err=%s" % ((i + 1) * 0.5, st["fps"], st["cap_ms"], st["nr_ms"], st["present_ms"], st["frames"], st["err"]))
send(p, {"cmd": "monitor", "action": "update", "cfg": {"half": False, "dump": ""}}); recv(p)
for i in range(6):
    time.sleep(0.5); send(p, {"cmd": "monitor", "action": "status"}); st, _ = recv(p)
    print("full %.1fs fps=%.1f cap=%.0f nr=%.0f present=%.0f frames=%d err=%s" % ((i + 1) * 0.5, st["fps"], st["cap_ms"], st["nr_ms"], st["present_ms"], st["frames"], st["err"]))
send(p, {"cmd": "monitor", "action": "stop"}); print("stop", recv(p)[0])
send(p, {"cmd": "quit"}); p.wait(timeout=10); print("exit", p.returncode)
