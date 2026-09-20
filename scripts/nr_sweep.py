"""Sweep the DLSS5 NR bridge on one input image; save results + a stats table.
usage: python nr_sweep.py <input.png> <outdir> [quick]
"""
import ctypes as C, os, sys, time
import numpy as np
from PIL import Image

ROOT = os.environ.get("DLSS5_NR_ROOT", r"C:\ComfyUI\custom_nodes\ComfyUI-DLSS5-NR")
lib = C.WinDLL(os.path.join(ROOT, "native", "bin", "dlss5nr_bridge.dll"))
RUNTIME = os.path.join(ROOT, "runtime")
lib.dlss5nr_init.argtypes = [C.c_int, C.c_wchar_p, C.c_char_p, C.c_int]
lib.dlss5nr_init.restype = C.c_int
lib.dlss5nr_process.argtypes = [
    C.POINTER(C.c_float), C.POINTER(C.c_float),
    C.c_int, C.c_int, C.c_int, C.c_int,
    C.c_float, C.c_float, C.c_float, C.c_float,
    C.c_int, C.c_int, C.c_int, C.c_char_p, C.c_int]
lib.dlss5nr_process.restype = C.c_int
lib.dlss5nr_version.restype = C.c_char_p
lib.dlss5nr_nvof_available.restype = C.c_int

err = C.create_string_buffer(4096)
rc = lib.dlss5nr_init(0, RUNTIME, err, len(err))
print("init rc=%d err=%s ver=%s nvof=%d" % (rc, err.value.decode("utf-8","replace"),
      lib.dlss5nr_version().decode(), lib.dlss5nr_nvof_available()), flush=True)
if rc == 0: os._exit(2)

src_path, outdir = sys.argv[1], sys.argv[2]
quick = len(sys.argv) > 3 and sys.argv[3] == "quick"
os.makedirs(outdir, exist_ok=True)
img = np.ascontiguousarray(np.asarray(Image.open(src_path).convert("RGB"), dtype=np.float32) / 255.0)
h, w = img.shape[:2]
print("src %dx%d mean=%.4f" % (w, h, img.mean()), flush=True)

def run(frame, style=0, preset=0, inten=1.0, tone=1.0, struct=1.0, skin=1.0, am=1, reset=1, temporal=0):
    out = np.zeros_like(frame)
    e = C.create_string_buffer(4096)
    t0 = time.perf_counter()
    rc = lib.dlss5nr_process(frame.ctypes.data_as(C.POINTER(C.c_float)), out.ctypes.data_as(C.POINTER(C.c_float)),
        w, h, style, preset, inten, tone, struct, skin, am, reset, temporal, e, len(e))
    ms = (time.perf_counter() - t0) * 1000
    if rc == 0: raise RuntimeError(e.value.decode("utf-8", "replace"))
    return out, ms

def lap_energy(a):
    g = a.mean(axis=2)
    l = -4*g[1:-1,1:-1] + g[:-2,1:-1] + g[2:,1:-1] + g[1:-1,:-2] + g[1:-1,2:]
    return float(np.abs(l).mean())

def stats(tag, out, ms, ref=img):
    d = np.abs(out - ref)
    line = "%-34s diff=%.4f max=%.3f mean=%.4f(src %.4f) sharp=%.5f(src %.5f) sat=%.4f(src %.4f) %.0fms" % (
        tag, d.mean(), d.max(), out.mean(), ref.mean(), lap_energy(out), lap_energy(ref),
        (out.max(2)-out.min(2)).mean(), (ref.max(2)-ref.min(2)).mean(), ms)
    print(line, flush=True)
    Image.fromarray((np.clip(out,0,1)*255).astype(np.uint8)).save(os.path.join(outdir, tag + ".png"))
    return line

lines = []
# 1. what the addon currently does (single pass, reset=1, preset 0, style 2, tone/struct/skin 1)
o, ms = run(img, style=2); lines.append(stats("A_addon_now_s2_p0_single", o, ms))
# 2. presets 0..3 (ComfyUI node default is preset 3, style natural)
for p in (0,1,2,3):
    for s in (0,1,2):
        if quick and not (p in (0,3)): continue
        o, ms = run(img, style=s, preset=p); lines.append(stats("B_s%d_p%d_single" % (s,p), o, ms))
# 3. temporal convergence: same frame N times, reset only on first, temporal=1 (NVOF) and temporal=0
for temporal in (0, 1):
    prev = None
    for i in range(16):
        o, ms = run(img, style=2, preset=3, reset=1 if i == 0 else 0, temporal=temporal)
        if i in (0, 1, 3, 7, 15):
            conv = float(np.abs(o - prev).mean()) if prev is not None else -1
            lines.append(stats("C_temporal%d_iter%02d" % (temporal, i+1), o, ms) + "  delta_prev=%.5f" % conv)
        prev = o
# 4. intensity / automask sweep at preset 3 style 2
for inten in (0.5, 1.0, 1.5, 2.0):
    o, ms = run(img, style=2, preset=3, inten=inten); lines.append(stats("D_s2_p3_int%.1f" % inten, o, ms))
o, ms = run(img, style=2, preset=3, am=0); lines.append(stats("D_s2_p3_nomask", o, ms))
for tone, struct, skin in ((0,0,0), (2,2,2), (1,2,1), (2,1,1), (1,1,-1)):
    o, ms = run(img, style=2, preset=3, tone=tone, struct=struct, skin=skin)
    lines.append(stats("D_s2_p3_t%g_st%g_sk%g" % (tone,struct,skin), o, ms))
with open(os.path.join(outdir, "sweep_stats.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("SWEEP_DONE", flush=True)
os._exit(0)
