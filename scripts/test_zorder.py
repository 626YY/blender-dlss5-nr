"""Owned-overlay z-order test: overlay must be visible to BitBlt over Blender, must be UNDER a window brought in
front of Blender, must follow Blender's minimize/restore, and must never sit in the TOPMOST band."""
import subprocess, json, sys, time, ctypes, ctypes.wintypes as W, os
import numpy as np
from PIL import Image
EXE = os.environ.get("DLSS5_LIVE_EXE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "native", "dlss5_live", "out", "dlss5_live.exe"))
DLLDIR = os.environ.get("DLSS5_DLL_DIR", r"C:\ComfyUI\custom_nodes\ComfyUI-DLSS5-NR\runtime")
HERE = os.path.dirname(os.path.abspath(__file__))
user32 = ctypes.windll.user32; gdi32 = ctypes.windll.gdi32; kernel32 = ctypes.windll.kernel32
VP = ctypes.c_void_p
user32.SetProcessDpiAwarenessContext(VP(-4))
user32.GetDC.restype = VP; user32.GetDC.argtypes = [VP]; user32.ReleaseDC.argtypes = [VP, VP]
user32.ClientToScreen.argtypes = [VP, ctypes.POINTER(W.POINT)]
user32.GetWindow.restype = VP; user32.GetWindow.argtypes = [VP, ctypes.c_uint]
user32.GetWindowTextW.argtypes = [VP, ctypes.c_wchar_p, ctypes.c_int]; user32.GetWindowTextLengthW.argtypes = [VP]
user32.GetClassNameW.argtypes = [VP, ctypes.c_wchar_p, ctypes.c_int]
user32.ShowWindow.argtypes = [VP, ctypes.c_int]; user32.DestroyWindow.argtypes = [VP]
user32.IsWindowVisible.argtypes = [VP]; user32.IsIconic.argtypes = [VP]
user32.GetWindowLongW.argtypes = [VP, ctypes.c_int]
user32.CreateWindowExW.restype = VP
user32.CreateWindowExW.argtypes = [W.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, W.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, VP, VP, VP, VP]
user32.SetWindowPos.argtypes = [VP, VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.PeekMessageW.argtypes = [ctypes.POINTER(W.MSG), VP, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
user32.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]; user32.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
kernel32.GetModuleHandleW.restype = VP
gdi32.CreateCompatibleDC.restype = VP; gdi32.CreateCompatibleDC.argtypes = [VP]
gdi32.SelectObject.restype = VP; gdi32.SelectObject.argtypes = [VP, VP]
gdi32.DeleteObject.argtypes = [VP]; gdi32.DeleteDC.argtypes = [VP]
gdi32.BitBlt.argtypes = [VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, VP, ctypes.c_int, ctypes.c_int, W.DWORD]
GW_HWNDPREV, GW_OWNER, GWL_EXSTYLE, WS_EX_TOPMOST = 3, 4, -20, 8

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
user32.GetForegroundWindow.restype = VP
blender_in_use = int(user32.GetForegroundWindow() or 0) == hwnd
if blender_in_use:
    print("note: Blender is the foreground window (in use): the cover step may be inconclusive and minimize/restore is skipped")
rect = [2, 26, 1353, 1056]
pt = W.POINT(rect[0], rect[1]); user32.ClientToScreen(VP(hwnd), ctypes.byref(pt))
sx, sy, w, h = pt.x, pt.y, rect[2], rect[3]


def shot(name):
    class BIH(ctypes.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD), ("biBitCount", W.WORD),
                    ("biCompression", W.DWORD), ("biSizeImage", W.DWORD), ("biX", W.LONG), ("biY", W.LONG), ("biU", W.DWORD), ("biI", W.DWORD)]
    class BI(ctypes.Structure):
        _fields_ = [("h", BIH), ("c", W.DWORD * 3)]
    gdi32.CreateDIBSection.restype = VP; gdi32.CreateDIBSection.argtypes = [VP, ctypes.POINTER(BI), ctypes.c_uint, ctypes.POINTER(VP), VP, W.DWORD]
    sdc = user32.GetDC(None); mdc = gdi32.CreateCompatibleDC(sdc)
    bi = BI(); bi.h.biSize = ctypes.sizeof(BIH); bi.h.biWidth = w; bi.h.biHeight = -h; bi.h.biPlanes = 1; bi.h.biBitCount = 32
    bits = VP(); hbm = gdi32.CreateDIBSection(mdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0); old = gdi32.SelectObject(mdc, hbm)
    gdi32.BitBlt(mdc, 0, 0, w, h, sdc, sx, sy, 0x00CC0020)
    arr = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8)), shape=(h, w, 4)).copy()[..., [2, 1, 0]]
    gdi32.SelectObject(mdc, old); gdi32.DeleteObject(hbm); gdi32.DeleteDC(mdc); user32.ReleaseDC(None, sdc)
    Image.fromarray(arr[::2, ::2]).save(os.path.join(HERE, "zorder_%s.png" % name))
    return arr


def overlays():
    """Top-level windows owned by the Blender window."""
    out = []
    def cb2(hw, lp):
        if user32.GetWindow(hw, GW_OWNER) == hwnd:
            cls = ctypes.create_unicode_buffer(64); user32.GetClassNameW(hw, cls, 64)
            out.append((int(hw), cls.value, bool(user32.IsWindowVisible(hw)), bool(user32.GetWindowLongW(hw, GWL_EXSTYLE) & WS_EX_TOPMOST)))
        return True
    user32.EnumWindows(proto(cb2), 0)
    return out


def above_blender():
    """Walk the z-order upward from Blender; list what sits above it (nearest first)."""
    res = []; cur = user32.GetWindow(VP(hwnd), GW_HWNDPREV); n = 0
    while cur and n < 12:
        if user32.IsWindowVisible(cur):
            cls = ctypes.create_unicode_buffer(64); user32.GetClassNameW(cur, cls, 64)
            k = user32.GetWindowTextLengthW(cur); buf = ctypes.create_unicode_buffer(k + 1); user32.GetWindowTextW(cur, buf, k + 1)
            res.append("%s[%s]%s" % (cls.value, buf.value[:20], " OWNED" if user32.GetWindow(cur, GW_OWNER) == hwnd else ""))
        cur = user32.GetWindow(cur, GW_HWNDPREV); n += 1
    return res


def pump(sec):
    end = time.time() + sec; msg = W.MSG()
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


log = open(os.path.join(HERE, "zorder_exe_stderr.txt"), "wb")
p = subprocess.Popen([EXE, "--dll-dir", DLLDIR, "--verbose"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1, encoding="utf-8")
def send(obj):
    p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush(); return json.loads(p.stdout.readline().strip() or "{}")
print("hello", json.loads(p.stdout.readline()))
cfg = dict(hwnd=hwnd, rect=rect, holes=[[0, 0, rect[2], 26]], split=0.0, view="SPLIT", half=True, style=2, tone=0.0, structure=2.0, skin=1.5,
           automask=True, mode="COLOR", strength=1.0)
print("start", send({"cmd": "start", "cfg": cfg})["ok"])
user32.SetWindowPos(VP(hwnd), None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # bring Blender to top of the normal band (no activate)
pump(3.0)
st = send({"cmd": "status"}); print("status frames=%d visible=%s err=%s" % (st["frames"], st["visible"], st["err"]))
base = shot("1_overlay")
print("overlays:", [(hex(a), b, "visible" if c else "hidden", "TOPMOST!" if d else "normal") for a, b, c, d in overlays()])
print("above Blender:", above_blender()[:4])
print("shot1 mean", base.mean().round(1))

# --- 2. a plain white window brought in front of Blender must cover the overlay
hinst = kernel32.GetModuleHandleW(None)
cover = user32.CreateWindowExW(0, "Static", "cover", 0x80000000 | 0x10000000 | 0x0006, sx - 10, sy - 10, w + 20, h + 20, None, None, hinst, None)  # WS_POPUP|WS_VISIBLE|SS_WHITERECT
user32.SetWindowPos(cover, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)   # HWND_TOP
pump(1.5)
covered = shot("2_covered")
above = above_blender()
print("above Blender (covered):", above[:4])
if any("Static[cover]" in s for s in above):
    print("shot2 mean", covered.mean().round(1), "-> white(>=250) means the overlay stayed inside Blender:", covered.mean() >= 250)
else:
    print("cover step INCONCLUSIVE: Windows put the cover window behind the active Blender window (run again with another app in front)")
user32.DestroyWindow(cover); pump(1.0)
back = shot("3_uncovered")
print("shot3 mean", back.mean().round(1), "diff vs shot1", np.abs(back.astype(int) - base.astype(int)).mean().round(2))

# --- 3. minimize / restore Blender: overlay must hide with it and come back (skipped while someone is working in Blender)
if blender_in_use:
    print("minimize/restore skipped (Blender in use)")
else:
    user32.ShowWindow(VP(hwnd), 6); pump(1.0)   # SW_MINIMIZE
    print("minimized:", bool(user32.IsIconic(VP(hwnd))), "overlays:", [("visible" if c else "hidden") for _, _, c, _ in overlays()], "status visible", send({"cmd": "status"})["visible"])
    user32.ShowWindow(VP(hwnd), 9); pump(2.0)   # SW_RESTORE
    print("restored:", not user32.IsIconic(VP(hwnd)), "overlays:", [("visible" if c else "hidden") for _, _, c, _ in overlays()], "status visible", send({"cmd": "status"})["visible"])
    final = shot("4_restored"); print("shot4 mean", final.mean().round(1), "diff vs shot1", np.abs(final.astype(int) - base.astype(int)).mean().round(2))
print("stop", send({"cmd": "stop"})["ok"])
p.wait(timeout=10); print("exit", p.returncode)
