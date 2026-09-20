"""Drive dlss5_live.exe: find Blender's window, start the live overlay on a rect, poll status, stop."""
import subprocess, json, sys, time, ctypes, ctypes.wintypes as W, os
EXE = os.environ.get("DLSS5_LIVE_EXE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "native", "dlss5_live", "out", "dlss5_live.exe"))
DLLDIR = os.environ.get("DLSS5_DLL_DIR", r"C:\ComfyUI\custom_nodes\ComfyUI-DLSS5-NR\runtime")
user32 = ctypes.windll.user32
VP = ctypes.c_void_p
user32.SetProcessDpiAwarenessContext(VP(-4))
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
user32.GetClientRect.argtypes = [VP, ctypes.POINTER(W.POINT)]; user32.IsIconic.argtypes = [VP]
def _area(h):
    r = W.RECT(); user32.GetClientRect(VP(h), ctypes.cast(ctypes.byref(r), ctypes.POINTER(W.POINT))); return 0 if user32.IsIconic(VP(h)) else r.right * r.bottom
found.sort(key=_area, reverse=True)   # several Blender windows: take the big, non-minimised one
hwnd = int(found[0]) if found else 0
for a in sys.argv:
    if a.startswith("--hwnd="):        # --hwnd=fg (the foreground window) or --hwnd=0x1234: drive any window
        user32.GetForegroundWindow.restype = VP
        hwnd = int(user32.GetForegroundWindow() or 0) if a[7:] == "fg" else int(a[7:], 0)
print("target hwnd", hex(hwnd), "blender windows", [hex(int(h)) for h in found])
args = [a for a in sys.argv[1:] if not a.startswith("--")]
rect = [int(x) for x in args[:4]] if len(args) >= 4 else [2, 26, 1353, 1056]
extra = ["--headless"] if "--headless" in sys.argv else []
half = "--full" not in sys.argv
seconds = float(args[4]) if len(args) >= 5 else 6.0
log = open(os.path.join(os.path.dirname(__file__), "live_exe_stderr.txt"), "wb")
p = subprocess.Popen([EXE, "--dll-dir", DLLDIR, "--verbose"] + extra, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1, encoding="utf-8")
def send(obj):
    p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()
    line = p.stdout.readline().strip()
    if not line:
        print("!! no reply (exe alive=%s)" % (p.poll() is None)); return {"fps": 0, "ms": 0, "nr_ms": 0, "cap_ms": 0, "frames": -1, "visible": False, "err": "no reply"}
    return json.loads(line)
t0 = time.time(); hello = json.loads(p.stdout.readline()); print("hello %.1fs" % (time.time() - t0), hello)
cfg = dict(hwnd=hwnd, rect=rect, holes=[[0, 0, rect[2], 26]], split=0.5, view="SPLIT", half=half, style=2, tone=0.0, structure=2.0, skin=1.5, automask=True, mode="COLOR", strength=1.0, bench=("--bench" in sys.argv))
if "--dump" in sys.argv:
    cfg["dump"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_gpu_dump.png").replace("\\", "/")
for a in sys.argv:
    if a.startswith("--cap="):          # --cap=auto|dwm|wgc|dda
        cfg["capture"] = a[6:]


def screen_shot(path):
    """BitBlt the overlay rect from the screen: shows whether recorders can see the overlay."""
    gdi32 = ctypes.windll.gdi32
    user32.GetDC.restype = VP; user32.GetDC.argtypes = [VP]; user32.ReleaseDC.argtypes = [VP, VP]
    user32.ClientToScreen.argtypes = [VP, ctypes.POINTER(W.POINT)]
    gdi32.CreateCompatibleDC.restype = VP; gdi32.CreateCompatibleDC.argtypes = [VP]
    gdi32.SelectObject.restype = VP; gdi32.SelectObject.argtypes = [VP, VP]
    gdi32.DeleteObject.argtypes = [VP]; gdi32.DeleteDC.argtypes = [VP]
    gdi32.BitBlt.argtypes = [VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, VP, ctypes.c_int, ctypes.c_int, W.DWORD]
    class BIH(ctypes.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD), ("biX", W.LONG), ("biY", W.LONG), ("biU", W.DWORD), ("biI", W.DWORD)]
    class BI(ctypes.Structure):
        _fields_ = [("h", BIH), ("c", W.DWORD * 3)]
    gdi32.CreateDIBSection.restype = VP; gdi32.CreateDIBSection.argtypes = [VP, ctypes.POINTER(BI), ctypes.c_uint, ctypes.POINTER(VP), VP, W.DWORD]
    pt = W.POINT(rect[0], rect[1]); user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = rect[2], rect[3]
    sdc = user32.GetDC(None); mdc = gdi32.CreateCompatibleDC(sdc)
    bi = BI(); bi.h.biSize = ctypes.sizeof(BIH); bi.h.biWidth = w; bi.h.biHeight = -h; bi.h.biPlanes = 1; bi.h.biBitCount = 32
    bits = VP(); hbm = gdi32.CreateDIBSection(mdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0); old = gdi32.SelectObject(mdc, hbm)
    gdi32.BitBlt(mdc, 0, 0, w, h, sdc, pt.x, pt.y, 0x00CC0020)
    import numpy as np
    from PIL import Image
    arr = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8)), shape=(h, w, 4)).copy()[..., [2, 1, 0]]
    Image.fromarray(arr).save(path)
    gdi32.SelectObject(mdc, old); gdi32.DeleteObject(hbm); gdi32.DeleteDC(mdc); user32.ReleaseDC(None, sdc)
    return arr
print("start", send({"cmd": "start", "cfg": cfg}))
end = time.time() + seconds
while time.time() < end:
    time.sleep(0.5)
    st = send({"cmd": "status"})
    print("%.1fs fps=%.1f frame=%.0fms nr=%.1fms cap=%.1fms frames=%d visible=%s err=%s" % (time.time() - t0, st["fps"], st["ms"], st["nr_ms"], st["cap_ms"], st["frames"], st["visible"], st["err"]))
if "--shot" in sys.argv:
    arr = screen_shot(os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_screen_shot.png"))
    print("screen shot saved", arr.shape)
if "--switch" in sys.argv:
    print("update FULL/NR", send({"cmd": "update", "cfg": {"mode": "FULL", "view": "NR"}}))
    time.sleep(1.5); st = send({"cmd": "status"}); print("after switch", st)
print("stop", send({"cmd": "stop"}))
try:
    p.wait(timeout=10)
except Exception:
    p.kill()
print("exit", p.returncode)
