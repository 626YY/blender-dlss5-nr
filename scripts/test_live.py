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
hwnd = int(found[0]); print("blender hwnd", hex(hwnd))
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
print("start", send({"cmd": "start", "cfg": cfg}))
end = time.time() + seconds
while time.time() < end:
    time.sleep(0.5)
    st = send({"cmd": "status"})
    print("%.1fs fps=%.1f frame=%.0fms nr=%.1fms cap=%.1fms frames=%d visible=%s err=%s" % (time.time() - t0, st["fps"], st["ms"], st["nr_ms"], st["cap_ms"], st["frames"], st["visible"], st["err"]))
if "--switch" in sys.argv:
    print("update FULL/NR", send({"cmd": "update", "cfg": {"mode": "FULL", "view": "NR"}}))
    time.sleep(1.5); st = send({"cmd": "status"}); print("after switch", st)
print("stop", send({"cmd": "stop"}))
try:
    p.wait(timeout=10)
except Exception:
    p.kill()
print("exit", p.returncode)
