"""纯 numpy 的图像数学,Blender 里和工作进程里共用。

约定:图像是 float32 (h, w, 3),顶行在前,显示域 0..1。
"""
import numpy as np


def luma(rgb):
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def box_blur2d(a, r):
    """三次盒滤波 ≈ 高斯。a: (h, w) float32。"""
    def along(x, axis):
        n = x.shape[axis]
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r, r)
        xp = np.pad(x, pad, mode="edge")
        cs = np.cumsum(xp, axis=axis, dtype=np.float64)
        zero = np.zeros_like(np.take(cs, [0], axis=axis))
        cs = np.concatenate([zero, cs], axis=axis)
        hi = np.take(cs, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
        lo = np.take(cs, np.arange(0, n), axis=axis)
        return ((hi - lo) / float(2 * r + 1)).astype(np.float32)

    for _ in range(3):
        a = along(along(a, 0), 1)
    return a


def low_pass(y, r):
    """大半径低通:先 4x4 平均缩小,再小半径盒滤波,再放大回去。
    只用来取低频项,块状放大看不出来;比全分辨率三次盒滤波快 10 倍以上。"""
    h, w = y.shape
    f = 4
    h2, w2 = h // f, w // f
    if h2 < 4 or w2 < 4:
        return box_blur2d(y, r)
    small = y[:h2 * f, :w2 * f].reshape(h2, f, w2, f).mean(axis=(1, 3)).astype(np.float32)
    small = box_blur2d(small, max(1, r // f))
    up = np.repeat(np.repeat(small, f, axis=0), f, axis=1)
    if up.shape != y.shape:                 # 边上不整除的几个像素补齐
        pad_h, pad_w = h - up.shape[0], w - up.shape[1]
        up = np.pad(up, ((0, pad_h), (0, pad_w)), mode="edge")
    return up


def composite(orig, nr, mode, k):
    """三种合成:
    COLOR  保留原色:取 NR 亮度、留原图色度       out = orig * (Y_nr / Y_orig)
    FULL   完整 NR:模型原始输出
    DETAIL 只加细节:只把 NR 的高频叠回原图        Y = Y_orig + (HF(Y_nr) - HF(Y_orig))
    k:0 = 原图,1 = 完整效果,>1 夸张化。
    """
    h = orig.shape[0]
    if mode == "FULL":
        out = nr
    elif mode == "COLOR":
        yo, yn = luma(orig), luma(nr)
        gain = np.clip(yn / np.maximum(yo, np.float32(0.004)), 0.0, 4.0)
        out = orig * gain[..., None]
    else:  # DETAIL
        r = max(2, int(round(6.0 * h / 1080.0)))
        yo, yn = luma(orig), luma(nr)
        # HF(nr) - HF(orig) = (yn - LP(yn)) - (yo - LP(yo)) = (yn - yo) - LP(yn - yo)
        d = yn - yo
        hf = d - low_pass(d, r)
        gain = np.clip((yo + hf) / np.maximum(yo, np.float32(0.004)), 0.0, 4.0)
        out = orig * gain[..., None]
    if abs(k - 1.0) > 1e-6:
        out = orig + np.float32(k) * (out - orig)
    return np.clip(out, 0.0, 1.0, out=out if out is not nr else None).astype(np.float32, copy=False)


def downsample(rgb, f):
    if f <= 1:
        return rgb
    h, w = rgb.shape[:2]
    h2, w2 = h // f, w // f
    return rgb[:h2 * f, :w2 * f].reshape(h2, f, w2, f, 3).mean(axis=(1, 3)).astype(rgb.dtype)


def fix_gpu_buffer(buf, h, w, dtype):
    """Blender 5.1 的 gpu.types.Buffer 走缓冲协议时步长是反的(列优先),
    np.asarray 直接读出来是条纹。底层内存其实是行优先 [h][w][4],
    这里按正确步长重新解释。返回连续的 (h, w, 4) 数组,行序和 GL 一样(底行在前)。"""
    from numpy.lib.stride_tricks import as_strided
    a = np.asarray(buf, dtype=dtype)
    isz = np.dtype(dtype).itemsize
    return np.ascontiguousarray(as_strided(a, shape=(h, w, 4), strides=(w * 4 * isz, 4 * isz, isz)))
