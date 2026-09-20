"""NR 监视窗:在工作进程里抓 Blender 窗口画面 -> NR -> 显示在盖住视口的点击穿透浮窗里。

为什么这样做:Blender 5.1 把 EEVEE 的画面在 Python 绘制回调之后才合成到视口,
回调里读不到;让 Blender 离屏重画一帧固定要 150ms+ 还卡主线程。而 Windows 的
PrintWindow(PW_RENDERFULLCONTENT)能直接拿到 DWM 合成好的 Blender 窗口画面
(16-30ms,不占 Blender 主线程),所以 EEVEE 照常全速跑,NR 在这边以自己的帧率
连续更新,显示在一个 WS_EX_LAYERED | WS_EX_TRANSPARENT 的浮窗里:
每像素 alpha 做左右分割对比,鼠标事件全部穿透给 Blender。

三级流水线,每级一个线程,只取最新帧(慢的一级会丢掉过时的帧,不排队):
  抓取:PrintWindow -> 裁视口矩形 -> (半分辨率时 2x2 平均)-> float32 RGB
  NR  :dlss5nr_process(temporal,不 reset)
  呈现:合成 -> 预乘 BGRA(分割 / 挖洞)-> UpdateLayeredWindow;窗口也由它创建和泵消息
吞吐 = 最慢一级的耗时;半分辨率时 NR ~20ms、呈现 ~35ms,约 20-25 帧/秒。
"""
import ctypes
import ctypes.wintypes as W
import threading
import time
import zlib

import numpy as np

import nr_math

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
VP = ctypes.c_void_p

# --- 64 位句柄安全的原型 ---
user32.GetDC.restype = VP
user32.GetDC.argtypes = [VP]
user32.ReleaseDC.argtypes = [VP, VP]
user32.PrintWindow.argtypes = [VP, VP, ctypes.c_uint]
user32.GetClientRect.argtypes = [VP, ctypes.POINTER(W.RECT)]
user32.GetWindowRect.argtypes = [VP, ctypes.POINTER(W.RECT)]
user32.ClientToScreen.argtypes = [VP, ctypes.POINTER(W.POINT)]
user32.IsWindow.argtypes = [VP]
user32.IsIconic.argtypes = [VP]
user32.GetForegroundWindow.restype = VP
user32.GetAncestor.restype = VP
user32.GetAncestor.argtypes = [VP, ctypes.c_uint]
user32.CreateWindowExW.restype = VP
user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, VP, VP, VP, VP]
user32.DestroyWindow.argtypes = [VP]
user32.ShowWindow.argtypes = [VP, ctypes.c_int]
user32.SetWindowPos.argtypes = [VP, VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.UpdateLayeredWindow.argtypes = [VP, VP, ctypes.POINTER(W.POINT), ctypes.POINTER(W.SIZE), VP,
                                       ctypes.POINTER(W.POINT), W.DWORD, VP, W.DWORD]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [VP, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.PeekMessageW.argtypes = [ctypes.POINTER(W.MSG), VP, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
user32.SetWindowDisplayAffinity.argtypes = [VP, W.DWORD]
gdi32.CreateCompatibleDC.restype = VP
gdi32.CreateCompatibleDC.argtypes = [VP]
gdi32.SelectObject.restype = VP
gdi32.SelectObject.argtypes = [VP, VP]
gdi32.DeleteObject.argtypes = [VP]
gdi32.DeleteDC.argtypes = [VP]
kernel32.GetModuleHandleW.restype = VP
kernel32.GetModuleHandleW.argtypes = [W.LPCWSTR]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", W.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


gdi32.CreateDIBSection.restype = VP
gdi32.CreateDIBSection.argtypes = [VP, ctypes.POINTER(BITMAPINFO), ctypes.c_uint, ctypes.POINTER(VP), VP, W.DWORD]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, VP, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", VP), ("hIcon", VP), ("hCursor", VP),
                ("hbrBackground", VP), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
HWND_TOPMOST = VP(-1)
ULW_ALPHA = 2
PM_REMOVE = 1
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 1, 2
WDA_EXCLUDEFROMCAPTURE = 0x11
GA_ROOT = 2
LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


class _DIB:
    """32 位顶行在前的 DIB section + 兼容 DC,像素以 numpy 数组暴露。"""

    def __init__(self, w, h):
        self.w, self.h = w, h
        screen = user32.GetDC(None)
        self.hdc = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount, bmi.bmiHeader.biCompression = 1, 32, 0
        bits = VP()
        self.hbm = gdi32.CreateDIBSection(self.hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not self.hbm:
            raise RuntimeError("CreateDIBSection 失败")
        self.old = gdi32.SelectObject(self.hdc, self.hbm)
        self.arr = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8)), shape=(h, w, 4))

    def free(self):
        try:
            gdi32.SelectObject(self.hdc, self.old)
            gdi32.DeleteObject(self.hbm)
            gdi32.DeleteDC(self.hdc)
        except Exception:                                   # noqa: BLE001
            pass


class _Slot:
    """单槽邮箱:生产者覆盖,消费者取走最新的。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.item = None
        self.seq = 0
        self.taken = -1

    def put(self, item):
        with self.lock:
            self.seq += 1
            self.item = item

    def take(self):
        with self.lock:
            if self.item is None or self.seq == self.taken:
                return None
            self.taken = self.seq
            return self.item


def _params_sig(cfg):
    return tuple(sorted((k, str(v)) for k, v in cfg.items() if k not in ("hwnd", "rect", "holes", "split", "view", "dump")))


class Monitor:
    def __init__(self, lib, ngx_lock):
        self.lib = lib
        self.ngx_lock = ngx_lock
        self.cfg = {}
        self.cfg_lock = threading.Lock()
        self.running = False
        self.err = ""
        self.stats = dict(fps=0.0, ms=0.0, nr_ms=0.0, cap_ms=0.0, present_ms=0.0, frames=0, size=(0, 0), visible=False)
        self._threads = []
        self._hwnd = None
        self._wndproc = None
        self._cls_registered = False
        self._stamps = []
        self._nr_size = (0, 0)
        self._slot_cap = _Slot()      # 抓取 -> NR :(crop_bgra, rgb_in, half, sig)
        self._slot_nr = _Slot()       # NR -> 呈现 :(crop_bgra, rgb_in, result, half, cfg_used)

    # ------------------------------------------------------------ 配置
    def start(self, cfg):
        if self.running:
            self.update(cfg)
            return True, ""
        with self.cfg_lock:
            self.cfg = dict(cfg)
        self.err = ""
        self.running = True
        self._nr_size = (0, 0)
        self._stamps = []
        self.stats["frames"] = 0
        self._slot_cap = _Slot()
        self._slot_nr = _Slot()
        self._threads = [threading.Thread(target=self._capture_loop, daemon=True),
                         threading.Thread(target=self._nr_loop, daemon=True),
                         threading.Thread(target=self._present_loop, daemon=True)]
        for t in self._threads:
            t.start()
        return True, ""

    def update(self, cfg):
        with self.cfg_lock:
            self.cfg.update(cfg)

    def stop(self):
        self.running = False
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    def status(self):
        d = dict(self.stats)
        d["err"] = self.err
        d["running"] = self.running
        return d

    def _get_cfg(self):
        with self.cfg_lock:
            return dict(self.cfg)

    # ------------------------------------------------------------ 1. 抓取线程
    def _capture_loop(self):
        dib = None
        last_sig = None
        try:
            while self.running:
                cfg = self._get_cfg()
                hwnd = cfg.get("hwnd")
                rect = cfg.get("rect")
                if not hwnd or not rect or not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
                    time.sleep(0.1)
                    continue
                if cfg.get("view") == "ORIG":
                    time.sleep(0.05)
                    continue
                rc = W.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rc))
                cw, ch = rc.right - rc.left, rc.bottom - rc.top
                if cw < 8 or ch < 8:
                    time.sleep(0.1)
                    continue
                if dib is None or (dib.w, dib.h) != (cw, ch):
                    if dib is not None:
                        dib.free()
                    dib = _DIB(cw, ch)
                t0 = time.perf_counter()
                ok = user32.PrintWindow(hwnd, dib.hdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
                if not ok:
                    self.err = "PrintWindow 失败"
                    time.sleep(0.2)
                    continue
                left, top, w, h = [int(v) for v in rect]
                left, top = max(0, left), max(0, top)
                w, h = min(w, cw - left), min(h, ch - top)
                if w < 16 or h < 16:
                    time.sleep(0.1)
                    continue
                crop = dib.arr[top:top + h, left:left + w]
                # 画面 + 参数没变就不往下送(采样哈希,~0.3ms)
                sig = (zlib.adler32(np.ascontiguousarray(crop[::7, ::13]).tobytes()), _params_sig(cfg), (w, h))
                if sig == last_sig and not cfg.get("bench"):
                    time.sleep(0.01)
                    continue
                last_sig = sig
                crop = crop.copy()
                half = bool(cfg.get("half", True))
                if half:
                    # 点采样缩小(2x2 平均要 40ms,点采样 1ms;增益图放大后乘回原图,差别看不出)
                    rgb_in = crop[::2, ::2, 2::-1].astype(np.float32)
                else:
                    rgb_in = crop[..., 2::-1].astype(np.float32)
                rgb_in *= np.float32(1.0 / 255.0)
                rgb_in = np.ascontiguousarray(rgb_in)
                self.stats["cap_ms"] = (time.perf_counter() - t0) * 1000.0
                self._slot_cap.put((crop, rgb_in, half, cfg))
                dt = time.perf_counter() - t0
                if dt < 0.025:
                    time.sleep(0.025 - dt)
        except Exception as exc:                            # noqa: BLE001
            self.err = "抓取线程: %s" % exc
        finally:
            if dib is not None:
                dib.free()

    # ------------------------------------------------------------ 2. NR 线程
    def _nr_loop(self):
        try:
            while self.running:
                item = self._slot_cap.take()
                if item is None:
                    time.sleep(0.003)
                    continue
                crop, rgb_in, half, cfg = item
                ih, iw = rgb_in.shape[:2]
                result = np.empty((ih, iw, 3), dtype=np.float32)
                reset = (iw, ih) != self._nr_size
                self._nr_size = (iw, ih)
                e = ctypes.create_string_buffer(4096)
                t0 = time.perf_counter()
                with self.ngx_lock:
                    rc = self.lib.dlss5nr_process(
                        rgb_in.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                        result.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                        iw, ih, int(cfg.get("style", 2)), 0, 1.0,
                        float(cfg.get("tone", 0.0)), float(cfg.get("structure", 2.0)), float(cfg.get("skin", 1.5)),
                        1 if cfg.get("automask", True) else 0, 1 if reset else 0, 1, e, len(e))
                if rc == 0:
                    self.err = "NR: %s" % e.value.decode("utf-8", "replace")
                    time.sleep(0.05)
                    continue
                self.err = ""
                mode = cfg.get("mode", "COLOR")
                k = float(cfg.get("strength", 1.0))
                if half:
                    # 半分辨率:这里就把合成 + 亮度增益算掉(NR 线程有余量),呈现线程只做放大 + 打包
                    out_low = nr_math.composite(rgb_in, result, mode, k)
                    gain = (out_low @ LUMA_W) / np.maximum(rgb_in @ LUMA_W, np.float32(0.004))
                    np.clip(gain, 0.0, 4.0, out=gain)
                    payload = (gain * 256.0).astype(np.uint16)      # 定点 8.8,后面用整数乘
                else:
                    payload = result
                self.stats["nr_ms"] = (time.perf_counter() - t0) * 1000.0
                self._slot_nr.put((crop, rgb_in, payload, half, cfg))
        except Exception as exc:                            # noqa: BLE001
            self.err = "NR 线程: %s" % exc

    # ------------------------------------------------------------ 3. 呈现线程(拥有窗口)
    def _create_window(self, owner):
        """浮窗做成 Blender 窗口的从属窗口(owner):Windows 保证它只在 Blender 正上方、跟着
        Blender 的前后层级走,别的程序盖住 Blender 它也被盖住,Blender 最小化它也藏。
        抓取用的是 PrintWindow(按窗口),浮窗不需要防截屏,录屏软件能看到它。"""
        hinst = kernel32.GetModuleHandleW(None)
        if not self._cls_registered:
            self._wndproc = WNDPROC(lambda h, m, wp, lp: user32.DefWindowProcW(h, m, wp, lp))
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = "DLSS5_NR_Monitor"
            user32.RegisterClassW(ctypes.byref(wc))       # 重复注册会失败(1410),无所谓
            self._cls_registered = True
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        hwnd = user32.CreateWindowExW(ex, "DLSS5_NR_Monitor", "DLSS 5 NR", WS_POPUP,
                                      0, 0, 8, 8, owner, None, hinst, None)
        if not hwnd:
            raise RuntimeError("CreateWindowExW 失败: %d" % ctypes.get_last_error())
        return hwnd

    def _pump(self):
        msg = W.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _present_loop(self):
        out_dib = None
        shown = False
        buf_f = None                 # 复用的全分辨率 float32 缓冲
        owner = None
        try:
            try:
                user32.SetProcessDpiAwarenessContext(VP(-4))
            except Exception:                               # noqa: BLE001
                pass
            while self.running:
                self._pump()
                cfg = self._get_cfg()
                view = cfg.get("view", "SPLIT")
                hwnd_b = cfg.get("hwnd")
                headless = bool(cfg.get("headless", False))
                if hwnd_b and user32.IsWindow(hwnd_b) and owner != hwnd_b:
                    if self._hwnd:
                        try:
                            user32.DestroyWindow(self._hwnd)
                        except Exception:                   # noqa: BLE001
                            pass
                        self._hwnd = None
                        shown = False
                    self._hwnd = self._create_window(hwnd_b)
                    owner = hwnd_b
                hide = (view == "ORIG" or not hwnd_b or not user32.IsWindow(hwnd_b) or user32.IsIconic(hwnd_b)
                        or not self._hwnd)
                if hide:
                    if shown and self._hwnd:
                        user32.ShowWindow(self._hwnd, SW_HIDE)
                        shown = False
                    self.stats["visible"] = False
                    time.sleep(0.05)
                    continue
                item = self._slot_nr.take()
                if item is None:
                    time.sleep(0.003)
                    continue
                crop, rgb_in, result, half, cfg_used = item
                t0 = time.perf_counter()
                h, w = crop.shape[:2]
                if out_dib is None or (out_dib.w, out_dib.h) != (w, h):
                    if out_dib is not None:
                        out_dib.free()
                    out_dib = _DIB(w, h)
                    buf_f = np.empty((h, w, 3), dtype=np.float32)
                self._compose(out_dib.arr, crop, rgb_in, result, half, cfg, buf_f)
                if cfg.get("dump"):
                    try:
                        np.save(cfg["dump"], out_dib.arr)
                    except Exception:                       # noqa: BLE001
                        pass
                if not headless:
                    left, top = int(cfg["rect"][0]), int(cfg["rect"][1])
                    pt = W.POINT(max(0, left), max(0, top))
                    user32.ClientToScreen(hwnd_b, ctypes.byref(pt))
                    dst = W.POINT(pt.x, pt.y)
                    src = W.POINT(0, 0)
                    size = W.SIZE(w, h)
                    blend = BLENDFUNCTION(0, 0, 255, 1)           # AC_SRC_OVER, AC_SRC_ALPHA
                    if not shown:
                        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                        shown = True
                    if not user32.UpdateLayeredWindow(self._hwnd, None, ctypes.byref(dst), ctypes.byref(size),
                                                      out_dib.hdc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA):
                        self.err = "UpdateLayeredWindow 失败: %d" % ctypes.get_last_error()
                    self.stats["visible"] = True
                now = time.perf_counter()
                self.stats["present_ms"] = (now - t0) * 1000.0
                self.stats["ms"] = self.stats["cap_ms"] + self.stats["nr_ms"] + self.stats["present_ms"]
                self.stats["frames"] += 1
                self.stats["size"] = (w, h)
                self._stamps = [t for t in self._stamps if now - t < 2.0] + [now]
                if len(self._stamps) > 1:
                    self.stats["fps"] = (len(self._stamps) - 1) / (self._stamps[-1] - self._stamps[0])
        except Exception as exc:                            # noqa: BLE001
            self.err = "呈现线程: %s" % exc
        finally:
            self.running = False
            if out_dib is not None:
                out_dib.free()
            if self._hwnd:
                try:
                    user32.DestroyWindow(self._hwnd)
                except Exception:                           # noqa: BLE001
                    pass
                self._hwnd = None
            self.stats["visible"] = False

    @staticmethod
    def _compose(dst_bgra, crop, rgb_in, payload, half, cfg, buf_f):
        """打包成预乘 BGRA。
        全分辨率:payload = NR 结果,这里 composite 后打包。
        半分辨率:payload = 低分辨率亮度增益(定点 8.8 uint16),放大 2x 用整数乘回全分辨率原图
        (底子保持清晰,细节来自 NR;全程 uint16,比 float 快一倍)。"""
        h, w = crop.shape[:2]
        view = cfg.get("view", "SPLIT")
        if not half:
            out = nr_math.composite(rgb_in, payload, cfg.get("mode", "COLOR"), float(cfg.get("strength", 1.0)))
            np.multiply(out, 255.0, out=buf_f)
            u8 = buf_f.astype(np.uint8)
            dst_bgra[..., 0] = u8[..., 2]
            dst_bgra[..., 1] = u8[..., 1]
            dst_bgra[..., 2] = u8[..., 0]
        else:
            gain = np.repeat(np.repeat(payload, 2, axis=0), 2, axis=1)[:h, :w]   # uint16,点采样时可能多 1 行/列
            gh, gw = gain.shape
            src = crop[:gh, :gw, :3].astype(np.uint16)
            src *= gain[..., None]
            src >>= 8
            np.minimum(src, 255, out=src)
            dst_bgra[:gh, :gw, :3] = src
            if gh < h:
                dst_bgra[gh:, :, :3] = crop[gh:, :, :3]
            if gw < w:
                dst_bgra[:, gw:, :3] = crop[:, gw:, :3]
        dst_bgra[..., 3] = 255
        if view == "SPLIT":
            split = float(cfg.get("split", 0.5))
            xs = max(0, min(w, int(round(w * split))))
            dst_bgra[:, :xs, :] = 0               # 预乘:alpha 0 的像素颜色也得是 0
            if 0 < xs < w:
                x0, x1 = max(0, xs - 1), min(w, xs + 1)
                dst_bgra[:, x0:x1, 0] = 30
                dst_bgra[:, x0:x1, 1] = 210
                dst_bgra[:, x0:x1, 2] = 255
                dst_bgra[:, x0:x1, 3] = 255
        # 挖洞:压在视口上的 Blender 面板/工具栏/标题栏区域透出去,UI 保持清晰
        for hole in cfg.get("holes", ()) or ():
            try:
                hx, hy, hw, hh = [int(v) for v in hole]
                x0, y0 = max(0, hx), max(0, hy)
                x1, y1 = min(w, hx + hw), min(h, hy + hh)
                if x1 > x0 and y1 > y0:
                    dst_bgra[y0:y1, x0:x1, :] = 0
            except Exception:                               # noqa: BLE001
                pass
