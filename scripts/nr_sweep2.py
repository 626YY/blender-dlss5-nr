"""Targeted sweep 2: can tone=0 keep the detail while dropping the darkening? plus detail-only composite test.
usage: python nr_sweep2.py <input.png> <outdir>"""
import ctypes as C, os, sys, time
import numpy as np
from PIL import Image
ROOT = os.environ.get("DLSS5_NR_ROOT", r"C:\ComfyUI\custom_nodes\ComfyUI-DLSS5-NR")
lib = C.WinDLL(os.path.join(ROOT, "native", "bin", "dlss5nr_bridge.dll")); RUNTIME = os.path.join(ROOT, "runtime")
lib.dlss5nr_init.argtypes = [C.c_int, C.c_wchar_p, C.c_char_p, C.c_int]; lib.dlss5nr_init.restype = C.c_int
lib.dlss5nr_process.argtypes = [C.POINTER(C.c_float), C.POINTER(C.c_float), C.c_int, C.c_int, C.c_int, C.c_int,
    C.c_float, C.c_float, C.c_float, C.c_float, C.c_int, C.c_int, C.c_int, C.c_char_p, C.c_int]; lib.dlss5nr_process.restype = C.c_int
err = C.create_string_buffer(4096); rc = lib.dlss5nr_init(0, RUNTIME, err, len(err)); print("init", rc, flush=True)
if rc == 0: os._exit(2)
src_path, outdir = sys.argv[1], sys.argv[2]; os.makedirs(outdir, exist_ok=True)
img = np.ascontiguousarray(np.asarray(Image.open(src_path).convert("RGB"), dtype=np.float32) / 255.0); h, w = img.shape[:2]

def run(frame, style=2, preset=0, inten=1.0, tone=1.0, struct=1.0, skin=1.0, am=1, iters=1, temporal=0):
    out = None
    for i in range(iters):
        out = np.zeros_like(frame); e = C.create_string_buffer(4096)
        rc = lib.dlss5nr_process(frame.ctypes.data_as(C.POINTER(C.c_float)), out.ctypes.data_as(C.POINTER(C.c_float)),
            w, h, style, preset, inten, tone, struct, skin, am, 1 if i == 0 else 0, temporal, e, len(e))
        if rc == 0: raise RuntimeError(e.value.decode("utf-8", "replace"))
    return out

def box_blur(a, r):
    # separable box blur via cumsum, applied on HxWxC
    def blur1(x, axis):
        n = x.shape[axis]; pad = [(0,0)]*x.ndim; pad[axis] = (r, r)
        xp = np.pad(x, pad, mode="edge"); cs = np.cumsum(xp, axis=axis, dtype=np.float64)
        cs = np.concatenate([np.zeros_like(np.take(cs, [0], axis=axis)), cs], axis=axis)
        hi = np.take(cs, np.arange(2*r+1, 2*r+1+n), axis=axis); lo = np.take(cs, np.arange(0, n), axis=axis)
        return ((hi - lo) / (2*r+1)).astype(np.float32)
    for _ in range(3): a = blur1(blur1(a, 0), 1)
    return a

def luma(a): return a[..., 0]*0.2126 + a[..., 1]*0.7152 + a[..., 2]*0.0722

def detail_only(orig, nr, k=1.0, r=6, luma_only=True):
    if luma_only:
        yo, yn = luma(orig), luma(nr)
        yo_b, yn_b = box_blur(yo[..., None], r)[..., 0], box_blur(yn[..., None], r)[..., 0]
        y_new = yo + k * ((yn - yn_b) - (yo - yo_b) * 0.0)   # add NR high-freq on top of original
        gain = np.clip(y_new / np.maximum(yo, 1e-3), 0.0, 4.0)
        return np.clip(orig * gain[..., None], 0, 1)
    ob, nb = box_blur(orig, r), box_blur(nr, r)
    return np.clip(ob + (nr - nb) * k + (orig - ob) * 0.0, 0, 1)   # replace orig HF with NR HF

def lap(a):
    g = a.mean(axis=2); l = -4*g[1:-1,1:-1] + g[:-2,1:-1] + g[2:,1:-1] + g[1:-1,:-2] + g[1:-1,2:]; return float(np.abs(l).mean())

def save(tag, out):
    d = np.abs(out - img)
    print("%-36s diff=%.4f mean=%.4f(src %.4f) sharp=%.5f(src %.5f) sat=%.4f(src %.4f)" % (tag, d.mean(), out.mean(), img.mean(),
          lap(out), lap(img), (out.max(2)-out.min(2)).mean(), (img.max(2)-img.min(2)).mean()), flush=True)
    Image.fromarray((np.clip(out,0,1)*255).astype(np.uint8)).save(os.path.join(outdir, tag + ".png"))

# raw model variants (style 2), tone off vs on
for tone, st, sk in ((0,2,2), (0,2,1.5), (0,1,1), (1,2,2), (2,2,2), (0,2,0)):
    save("E_s2_t%g_st%g_sk%g" % (tone, st, sk), run(img, tone=tone, struct=st, skin=sk))
# style 1 natural at strong structure
save("E_s1_t0_st2_sk2", run(img, style=1, tone=0, struct=2, skin=2))
save("E_s0_t0_st2_sk2", run(img, style=0, tone=0, struct=2, skin=2))
# detail-only composites from the strongest raw output
nr = run(img, tone=2, struct=2, skin=2)
for k in (1.0, 1.5): 
    save("F_detail_luma_k%.1f_r6" % k, detail_only(img, nr, k=k, r=6, luma_only=True))
    save("F_detail_rgb_k%.1f_r6" % k, detail_only(img, nr, k=k, r=6, luma_only=False))
save("F_detail_luma_k1.0_r12", detail_only(img, nr, k=1.0, r=12, luma_only=True))
save("F_detail_luma_k1.0_r3", detail_only(img, nr, k=1.0, r=3, luma_only=True))
nr0 = run(img, tone=0, struct=2, skin=2)
save("F_detail_luma_k1.0_r6_fromT0", detail_only(img, nr0, k=1.0, r=6, luma_only=True))
print("SWEEP2_DONE", flush=True); os._exit(0)
