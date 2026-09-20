"""Test the v0.9 worker protocol: rgba8 in (GL bottom-up) -> composite in worker -> rgba32f_gl out; temporal reset flags."""
import subprocess, struct, json, sys, time, os
import numpy as np
from PIL import Image
MAGIC = b"NRW1"
BPY = os.environ.get("BLENDER_PYTHON", r"C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe")
WORKER, SRC, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
ROOT = os.environ.get("DLSS5_NR_ROOT", r"C:\ComfyUI\custom_nodes\ComfyUI-DLSS5-NR")


def send(p, obj, payload=b""):
    if payload:
        obj["bytes"] = len(payload)
    js = json.dumps(obj).encode()
    p.stdin.write(MAGIC + struct.pack("<I", len(js)) + js + payload)
    p.stdin.flush()


def rexact(f, n):
    b = b""
    while len(b) < n:
        c = f.read(n - len(b))
        if not c:
            return None
        b += c
    return b


def recv(p):
    h = rexact(p.stdout, 8)
    assert h and h[:4] == MAGIC, h
    (n,) = struct.unpack("<I", h[4:])
    obj = json.loads(rexact(p.stdout, n))
    pl = rexact(p.stdout, obj.get("bytes", 0)) if obj.get("bytes") else b""
    return obj, pl


p = subprocess.Popen([BPY, "-u", WORKER, ROOT, "0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=open(OUT + ".stderr.txt", "wb"))
print("hello", recv(p)[0])
img = np.asarray(Image.open(SRC).convert("RGB"), dtype=np.float32) / 255.0
h, w = img.shape[:2]
# GL-style input: uint8 RGBA, bottom row first
rgba8 = np.concatenate([(img[::-1] * 255 + 0.5).astype(np.uint8), np.full((h, w, 1), 255, np.uint8)], axis=2).tobytes()
base = dict(w=w, h=h, style=2, tone=0.0, structure=2.0, skin=1.5, automask=True, temporal=1, iters=1)
for i, (tag, extra) in enumerate((("live_reset_COLOR", {"reset": 1, "mode": "COLOR", "strength": 1.0}),
                                  ("live_noreset_COLOR", {"reset": 0, "mode": "COLOR", "strength": 1.0}),
                                  ("live_noreset_DETAIL", {"reset": 0, "mode": "DETAIL", "strength": 1.0}),
                                  ("live_noreset_FULL", {"reset": 0, "mode": "FULL", "strength": 1.0}))):
    req = dict(base, **extra); req["in"] = "rgba8"; req["out"] = "rgba32f_gl"
    t0 = time.time(); send(p, req, rgba8); obj, pl = recv(p); dt = (time.time() - t0) * 1000
    assert obj.get("ok"), obj
    out = np.frombuffer(pl, dtype=np.float32).reshape(h, w, 4)[::-1, :, :3]   # back to top-down
    print("%-22s roundtrip %.0fms  worker %.0fms  nr %.0fms  diff=%.4f alpha=%.2f" % (
        tag, dt, obj["ms"], obj["nr_ms"], float(np.abs(out - img).mean()), float(np.frombuffer(pl, np.float32).reshape(h, w, 4)[..., 3].mean())))
    Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)).save(OUT + "_" + tag + ".png")
# legacy path still works
req = dict(base); req.pop("temporal"); t0 = time.time(); send(p, req, np.ascontiguousarray(img, np.float32).tobytes()); obj, pl = recv(p)
print("legacy rgb32f", obj, "%.0fms" % ((time.time() - t0) * 1000))
send(p, {"cmd": "quit"}); p.wait(timeout=10); print("exit", p.returncode)
