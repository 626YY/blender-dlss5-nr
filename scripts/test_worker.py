"""Drive nr_worker.py from a plain Python and compare against the in-process sweep result."""
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


t0 = time.time()
p = subprocess.Popen([BPY, "-u", WORKER, ROOT, "0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=open(OUT + ".worker_stderr.txt", "wb"))
hello, _ = recv(p)
print("hello %.1fs" % (time.time() - t0), hello)
img = np.ascontiguousarray(np.asarray(Image.open(SRC).convert("RGB"), dtype=np.float32) / 255.0)
h, w = img.shape[:2]
for tag, req in (("s2_t2_st2_sk2", dict(style=2, tone=2, structure=2, skin=2, iters=1)),
                 ("s2_t0_st2_sk1.5_it4", dict(style=2, tone=0, structure=2, skin=1.5, iters=4, temporal=1))):
    t1 = time.time()
    send(p, dict(w=w, h=h, automask=True, **req), img.tobytes())
    resp, pl = recv(p)
    dt = time.time() - t1
    print(tag, resp, "roundtrip %.0fms" % (dt * 1000))
    out = np.frombuffer(pl, dtype=np.float32).reshape(h, w, 3)
    Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)).save(OUT + "_" + tag + ".png")
    if tag == "s2_t2_st2_sk2":
        ref = np.asarray(Image.open(os.path.join(os.path.dirname(SRC), "sweep2_vp", "E_s2_t2_st2_sk2.png")),
                         dtype=np.float32) / 255.0
        print("  vs in-process sweep result: mean|diff|=%.5f" % float(np.abs(out - ref).mean()))
send(p, {"cmd": "quit"})
p.wait(timeout=10)
print("worker exit", p.returncode)
