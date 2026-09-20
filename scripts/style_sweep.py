import ctypes as C, os, sys, time
import numpy as np
from PIL import Image
ROOT = os.environ.get("DLSS5_NR_ROOT", r"C:\ComfyUI\custom_nodes\ComfyUI-DLSS5-NR")
lib = C.WinDLL(os.path.join(ROOT, "native", "bin", "dlss5nr_bridge.dll")); RUNTIME = os.path.join(ROOT, "runtime")
lib.dlss5nr_init.argtypes = [C.c_int, C.c_wchar_p, C.c_char_p, C.c_int]; lib.dlss5nr_init.restype = C.c_int
lib.dlss5nr_process.argtypes = [C.POINTER(C.c_float), C.POINTER(C.c_float), C.c_int, C.c_int, C.c_int, C.c_int,
    C.c_float, C.c_float, C.c_float, C.c_float, C.c_int, C.c_int, C.c_int, C.c_char_p, C.c_int]; lib.dlss5nr_process.restype = C.c_int
err = C.create_string_buffer(4096); rc = lib.dlss5nr_init(0, RUNTIME, err, len(err)); print("init", rc, flush=True)
src, outdir = sys.argv[1], sys.argv[2]; os.makedirs(outdir, exist_ok=True)
img = np.ascontiguousarray(np.asarray(Image.open(src).convert("RGB"), dtype=np.float32) / 255.0); h, w = img.shape[:2]
outs = {}
for style in range(0, 10):
    out = np.zeros_like(img); e = C.create_string_buffer(4096)
    rc = lib.dlss5nr_process(img.ctypes.data_as(C.POINTER(C.c_float)), out.ctypes.data_as(C.POINTER(C.c_float)),
        w, h, style, 0, 1.0, 0.0, 2.0, 1.5, 1, 1, 0, e, len(e))
    if rc == 0:
        print("style %d FAIL: %s" % (style, e.value.decode("utf-8", "replace")[:80]), flush=True); continue
    outs[style] = out
    d = np.abs(out - img).mean(); same = [s2 for s2, o2 in outs.items() if s2 != style and np.abs(o2 - out).mean() < 1e-4]
    print("style %d  diff_vs_src=%.4f mean=%.4f sat=%.4f  identical_to=%s" % (style, d, out.mean(), (out.max(2)-out.min(2)).mean(), same), flush=True)
    Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)).save(os.path.join(outdir, "style%d.png" % style))
os._exit(0)
