"""DLSS 5 神经渲染(NGX Feature 18)Blender 集成 + EEVEE 引导通道捕获

v0.8.0 —— 重写神经渲染这一半。实测(RTX 4080 SUPER / 驱动 616.64 / 模型 310.8.SF /
Blender 5.1.2)后发现的事实,本版设计全部基于它们:

  * 模型对 1080p 视口图效果非常明显(皮肤毛孔、睫毛、唇纹、鼻孔都会长出来),
    之前"看不出变化"是因为喂的是 480x270 小图 + 叠加层没和视口对齐。
  * `tone`(色调重打光)是让整张图发灰发暗的元凶,0 时细节全在、颜色基本不跑。
  * `intensity` 在 >=1 时完全无效,<1 只是整体变暗;`preset` 0..3 输出逐位相同。
    两个假旋钮已删掉。
  * 同一帧反复迭代(temporal)只带来 ~1% 的锐度提升,默认 1 次即可。

本版做法:
  1. 视口抓图用 render.opengl(视口渲染),渲染分辨率临时设成「目标矩形 x 倍率」:
     透视视角 = 整个视口区域,相机视角 = 相机框。这样叠加层和视口像素一一对齐,
     还能 2 倍超采样喂模型。(GPUOffScreen.draw_view3d 在 5.1.2 + OpenGL 后端上
     实测只吐出黑白交替行,Solid / 材质预览都一样,弃用。)
     注意:视口渲染会覆盖 Render Result,所以「NR 上一帧渲染」要在 F12 之后紧接着用。
  2. NGX 跑在独立的工作进程里(Blender 自带 python.exe + nr_worker.py):
     驱动崩溃/死锁只死小进程,Blender 和未保存的工程不受影响;停用扩展就能
     释放显存(进程内加载永远卸不掉)。
  3. 三种合成方式:
       保留原色 —— 取模型的亮度细节,保留原图色度(默认,最不毁打光)
       完整 NR  —— 模型原始输出(想要它的"重打光"就选这个)
       只加细节 —— 只把模型的高频细节叠回原图,低频光照原封不动
     改合成方式/强度不需要重新跑模型,立刻刷新。
  4. 叠加层带对比分割线(左原图 / 右 NR),相机视角时只画在相机框内。
     v0.8.1:黄线可以在视口里直接拖(靠近按住左键),面板上有 分割对比 / 只看 NR /
     只看原图 三态切换;想绑快捷键在偏好设置里给 dlssnr.cycle_view 绑一个。
  5. v0.9 实时模式 = 工作进程里的「NR 监视窗」(nr_monitor.py):Blender 5.1 把 EEVEE
     画面在 Python 绘制回调之后才合成到视口,回调里读不到;离屏重画又固定 150ms+ 且卡
     主线程。所以改由工作进程用 PrintWindow 抓 Blender 窗口(DWM 合成后的最终画面,
     不占 Blender 主线程)-> NR -> 显示在盖住视口的点击穿透浮窗里。EEVEE 照常全速跑,
     NR 以自己的帧率连续更新(1353x1056 约 8 帧/秒,半分辨率约 15+)。
  5. F12 渲染结果同样可以处理,写进图像数据块「DLSS5_NR」,可另存 PNG。

引导通道捕获(EEVEE 深度/法线/矢量 -> 多层 EXR)保留在折叠子面板里,逻辑未动。
注意:EEVEE 的运动矢量通道实测恒为零,见 REPORT。
"""
import array
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from . import nr_math

bl_info = {
    "name": "DLSS 5 神经渲染 + EEVEE 引导通道",
    "author": "built in-session",
    "version": (0, 10, 2),
    "blender": (5, 0, 0),
    "location": "3D 视图 > 侧边栏 > DLSS NR",
    "description": "DLSS 5 神经渲染(视口 / F12 结果),以及 EEVEE 引导通道捕获",
    "category": "Render",
}

GUIDE_SOCKETS = ("Image", "Alpha", "Depth", "Normal", "Vector")
NODE_GROUP_NAME = "DLSSNR_Guides"
MANIFEST_NAME = "guides_manifest.txt"
NR_IMAGE = "DLSS5_NR"
NR_IMAGE_ORIG = "DLSS5_NR_原图"
MAGIC = b"NRW1"

GUIDE_ITEMS = [
    ("Image", "颜色图", "EEVEE 合成结果"),
    ("Depth", "深度图", "几何深度"),
    ("Normal", "法线图", "表面法线"),
    ("Vector", "运动矢量", "注意:EEVEE 实测始终写零"),
    ("Alpha", "Alpha", "物体遮罩"),
]

STYLE_ITEMS = [
    ("2", "电影感", "细节最扎实;配合色调=0 颜色基本不跑"),
    ("1", "自然", "锐度最高,略偏亮"),
    ("0", "原生", "会去饱和,一般不用"),
]

MODE_ITEMS = [
    ("COLOR", "保留原色", "取模型的亮度细节,保留原图色度。最不毁打光,默认"),
    ("FULL", "完整 NR", "模型原始输出。想要它的重打光/风格化就选这个"),
    ("DETAIL", "只加细节", "只把模型的高频细节叠回原图,低频光照原封不动"),
]

SCALE_ITEMS = [
    ("AUTO", "自动", "视口高度 < 900 时 2 倍超采样,否则 1 倍"),
    ("1", "1x", "按视口像素抓"),
    ("2", "2x", "2 倍超采样喂模型(更细,慢一点)"),
]

DEFAULT_NR_ROOT = ""   # ComfyUI-DLSS5-NR 的安装目录(含 native/bin 与 runtime),在「高级」里填

# 风格预设:模型只有 0/1/2 三种真风格(3..9 实测和 2 逐位相同),
# 真正拉开差别的是 色调 / 结构 / 皮肤 / 合成方式 的组合,所以预设按组合来。
#          (id,         名字,               说明,                                  style, tone, struct, skin, mode,     k)
PRESETS = [
    ("TEXTURE",  "质感·保留原色", "皮肤毛孔/唇纹/睫毛,颜色和打光不动。默认",          2, 0.0, 2.0, 1.5, "COLOR",  1.0),
    ("SHARP",    "自然·锐利",     "自然风格,细节最锐,肤色略提亮",                    1, 0.0, 2.0, 2.0, "COLOR",  1.0),
    ("SOFT",     "柔和·轻微",     "结构 1、强度 0.7,只微微加一点真实感",              2, 0.0, 1.0, 1.0, "COLOR",  0.7),
    ("CINEMA",   "电影感·重打光", "模型原始输出 + 最强重打光,整体偏暗偏灰",           2, 2.0, 2.0, 1.5, "FULL",   1.0),
    ("CINEMA_L", "电影感·轻打光", "模型原始输出,重打光减半",                         2, 1.0, 2.0, 1.5, "FULL",   1.0),
    ("REALIST",  "写实·去饱和",   "原生风格,会去饱和、更像照片",                     0, 1.0, 2.0, 1.5, "FULL",   1.0),
    ("DETAIL_L", "只加细节·轻",   "只把高频细节叠回原图,光照原封不动,强度 0.7",       2, 0.0, 2.0, 1.5, "DETAIL", 0.7),
    ("DETAIL_H", "只加细节·强",   "自然风格的高频细节,强度 1.3,会有点颗粒感",         1, 0.0, 2.0, 2.0, "DETAIL", 1.3),
    ("CUSTOM",   "自定义",        "不改任何参数,手动调「模型参数」",                  None, None, None, None, None, None),
]
PRESET_ITEMS = [(p[0], p[1], p[2]) for p in PRESETS]


# ==========================================================================
# 工作进程客户端(NGX 在 Blender 之外)
# ==========================================================================
class _Worker:
    proc = None
    state = "off"          # off / ok / fail
    err = ""
    info = {}
    root = ""
    log_path = os.path.join(tempfile.gettempdir(), "dlss5_nr_worker.log")
    _log = None

    @classmethod
    def _python(cls):
        cands = [os.path.join(sys.prefix, "bin", "python.exe")]
        try:
            ver = "%d.%d" % tuple(bpy.app.version[:2])
            cands.append(os.path.join(os.path.dirname(bpy.app.binary_path), ver, "python", "bin", "python.exe"))
        except Exception:                                   # noqa: BLE001
            pass
        for c in cands:
            if os.path.exists(c):
                return c
        return None

    io_lock = threading.Lock()   # 同一时刻只允许一次 请求→回应 往返(同步或异步)

    @classmethod
    def alive(cls):
        return cls.proc is not None and cls.proc.poll() is None

    @classmethod
    def ensure(cls, root, timeout=60.0):
        if cls.alive() and cls.state == "ok" and cls.root == root:
            return True
        cls.stop()
        exe = cls._python()
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nr_worker.py")
        if exe is None:
            cls.state, cls.err = "fail", "找不到 Blender 自带的 python.exe"
            return False
        if not os.path.exists(worker):
            cls.state, cls.err = "fail", "缺少 nr_worker.py"
            return False
        try:
            cls._log = open(cls.log_path, "wb")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            cls.proc = subprocess.Popen(
                [exe, "-u", worker, root, "0"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=cls._log,
                creationflags=flags)
        except Exception as exc:                            # noqa: BLE001
            cls.state, cls.err = "fail", "启动工作进程失败: %s" % exc
            return False
        hello = cls._recv(timeout)
        if hello is None or hello[0] is None:
            cls.state, cls.err = "fail", "工作进程无响应(见 %s)" % cls.log_path
            cls.stop()
            return False
        if not hello[0].get("ok"):
            cls.state, cls.err = "fail", hello[0].get("err", "未知错误")
            cls.stop()
            return False
        cls.info = hello[0]
        cls.root = root
        cls.state, cls.err = "ok", ""
        return True

    @classmethod
    def stop(cls):
        p = cls.proc
        cls.proc = None
        if p is not None:
            try:
                if p.poll() is None:
                    cls._send_raw(p, {"cmd": "quit"})
                    try:
                        p.wait(timeout=1.5)
                    except Exception:                       # noqa: BLE001
                        p.kill()
            except Exception:                               # noqa: BLE001
                try:
                    p.kill()
                except Exception:                           # noqa: BLE001
                    pass
        if cls._log is not None:
            try:
                cls._log.close()
            except Exception:                               # noqa: BLE001
                pass
            cls._log = None
        if cls.state == "ok":
            cls.state = "off"

    @staticmethod
    def _send_raw(p, obj, payload=b""):
        if payload:
            obj["bytes"] = len(payload)
        js = json.dumps(obj).encode("utf-8")
        p.stdin.write(MAGIC + struct.pack("<I", len(js)) + js)
        if payload:
            p.stdin.write(payload)
        p.stdin.flush()

    @classmethod
    def _recv(cls, timeout):
        """带超时的完整读取;超时视为工作进程死锁,由调用方重启。"""
        p = cls.proc
        if p is None:
            return None
        box = []

        def rd():
            try:
                f = p.stdout

                def exact(n):
                    chunks, got = [], 0
                    while got < n:
                        c = f.read(n - got)
                        if not c:
                            return None
                        chunks.append(c)
                        got += len(c)
                    return b"".join(chunks)

                head = exact(8)
                if head is None or head[:4] != MAGIC:
                    box.append((None, None))
                    return
                (n,) = struct.unpack("<I", head[4:8])
                js = exact(n)
                if js is None:
                    box.append((None, None))
                    return
                obj = json.loads(js.decode("utf-8"))
                nb = int(obj.get("bytes", 0))
                payload = exact(nb) if nb else b""
                box.append((obj, payload))
            except Exception:                               # noqa: BLE001
                box.append((None, None))

        t = threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive() or not box:
            return None
        return box[0]

    @classmethod
    def exchange(cls, req, payload, timeout=60.0):
        """一次完整的 请求→回应。返回 (obj, payload) 或 (None, 错误字符串)。线程安全。"""
        if not cls.alive():
            return None, "工作进程未运行"
        with cls.io_lock:
            try:
                cls._send_raw(cls.proc, req, payload)
            except Exception as exc:                        # noqa: BLE001
                cls.stop()
                cls.state, cls.err = "fail", "发送帧失败(工作进程可能已崩溃): %s" % exc
                return None, cls.err
            r = cls._recv(timeout)
        if r is None or r[0] is None:
            cls.stop()
            cls.state, cls.err = "fail", "NR 超时或工作进程崩溃,已停止;再点一次会自动重启"
            return None, cls.err
        obj, data = r
        if not obj.get("ok"):
            return None, obj.get("err", "未知错误")
        cls.info["last_ms"] = obj.get("ms", 0.0)
        cls.info["last_nr_ms"] = obj.get("nr_ms", 0.0)
        return obj, data

    @classmethod
    def process(cls, rgb, params, timeout=60.0):
        """同步:rgb float32 (h, w, 3) 顶行在前 -> (nr 数组 或 None, 错误)。"""
        import numpy as np
        h, w = rgb.shape[:2]
        req = dict(w=int(w), h=int(h), **params)
        obj, data = cls.exchange(req, np.ascontiguousarray(rgb, dtype=np.float32).tobytes(), timeout)
        if obj is None:
            return None, data
        return np.frombuffer(data, dtype=np.float32).reshape(h, w, 3), ""


# ==========================================================================
# 图像数学(numpy,顶行在前)
# ==========================================================================
_composite = nr_math.composite
_downsample = nr_math.downsample


def _write_png(path, rgb01):
    import numpy as np
    u8 = (np.clip(rgb01, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    h, w = u8.shape[:2]
    rows = np.concatenate([np.zeros((h, 1), np.uint8), u8.reshape(h, w * 3)], axis=1)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)))
        fh.write(chunk(b"IEND", b""))


def _to_image(name, rgb01):
    """写进浮点 Non-Color 图像数据块(图像编辑器按原值显示)。"""
    import numpy as np
    h, w = rgb01.shape[:2]
    img = bpy.data.images.get(name)
    if img is not None and (tuple(img.size) != (w, h) or not img.is_float):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, w, h, alpha=False, float_buffer=True)
        try:
            img.colorspace_settings.name = "Non-Color"
        except Exception:                                   # noqa: BLE001
            pass
    rgba = np.concatenate([rgb01[::-1], np.ones((h, w, 1), np.float32)], axis=2)
    img.pixels.foreach_get  # noqa: B018  (确保属性存在)
    img.pixels.foreach_set(np.ascontiguousarray(rgba, dtype=np.float32).ravel())
    img.update()
    return img


# ==========================================================================
# 抓取
# ==========================================================================
def _load_png_rgb(path):
    """PNG -> float32 (h, w, 3) 顶行在前、显示域原值(Non-Color 载入,不做任何色彩变换)。"""
    import numpy as np
    img = bpy.data.images.load(path, check_existing=False)
    try:
        try:
            img.colorspace_settings.name = "Non-Color"
        except Exception:                                   # noqa: BLE001
            pass
        w, h = img.size
        if w == 0 or h == 0:
            return None
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(img)
    rgb = buf.reshape(h, w, 4)[::-1, :, :3]
    return np.ascontiguousarray(np.clip(rgb, 0.0, 1.0), dtype=np.float32)


def _target_rect(region, rv3d, scene):
    """叠加层要覆盖的区域像素矩形:相机视角 = 相机框,否则 = 整个区域。"""
    rect = _camera_rect(region, rv3d, scene)
    if rect is None:
        rect = (0, 0, region.width, region.height)
    return rect


def _clip_rect(rect, region):
    """矩形和区域求交,得到能从帧缓冲里读、能画在屏幕上的部分。"""
    x0, y0 = max(rect[0], 0), max(rect[1], 0)
    x1, y1 = min(rect[0] + rect[2], region.width), min(rect[1] + rect[3], region.height)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return (0, 0, region.width, region.height)
    return (x0, y0, x1 - x0, y1 - y0)


def _capture_viewport(win, area, region, scale):
    """GPU 离屏画当前视口 -> (float32 (H, W, 3) 顶行在前显示域, 目标矩形, 实际倍率)。

    整个区域按 scale 倍离屏渲染(不落盘、不改场景、不碰 Render Result),
    再裁出目标矩形:透视视角 = 整个区域,相机视角 = 相机框(和区域求交)。
    离屏失败时退回 render.opengl 路线。
    """
    import gpu
    import numpy as np
    scene = win.scene
    space = area.spaces.active
    rv3d = space.region_3d
    rect = _clip_rect(_target_rect(region, rv3d, scene), region)
    while scale > 1 and max(region.width, region.height) * scale > 4096:
        scale -= 1
    W, H = region.width * scale, region.height * scale
    try:
        off = gpu.types.GPUOffScreen(W, H, format="RGBA8")
        try:
            with bpy.context.temp_override(window=win, area=area, region=region):
                off.draw_view3d(win.scene, win.view_layer, space, region,
                                rv3d.view_matrix, rv3d.window_matrix,
                                do_color_management=True)
            buf = off.texture_color.read()
        finally:
            off.free()
        a = nr_math.fix_gpu_buffer(buf, H, W, np.uint8)          # (H, W, 4) 底行在前
        x0, y0, rw, rh = rect
        sub = a[y0 * scale:(y0 + rh) * scale, x0 * scale:(x0 + rw) * scale, :3][::-1]
        rgb = np.ascontiguousarray(sub, dtype=np.float32) * (1.0 / 255.0)
        return rgb, rect, scale
    except Exception:                                       # noqa: BLE001
        return _capture_viewport_opengl(win, area, region, scale, rect)


def _capture_viewport_opengl(win, area, region, scale, rect):
    """备用:render.opengl 落盘再读回(会覆盖 Render Result)。"""
    scene = win.scene
    W, H = max(8, rect[2] * scale), max(8, rect[3] * scale)
    r = scene.render
    ims = r.image_settings
    saved = (r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
             ims.file_format, ims.color_depth, ims.color_mode)
    tmp = os.path.join(tempfile.gettempdir(), "dlss5_viewport_in.png")
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        r.resolution_x, r.resolution_y, r.resolution_percentage = W, H, 100
        r.filepath = tmp
        ims.file_format, ims.color_depth, ims.color_mode = "PNG", "8", "RGB"
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    finally:
        (r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
         ims.file_format, ims.color_depth, ims.color_mode) = saved
    if not os.path.exists(tmp):
        raise RuntimeError("视口渲染没有产出文件")
    rgb = _load_png_rgb(tmp)
    if rgb is None:
        raise RuntimeError("视口渲染读回为空")
    return rgb, rect, scale


def _capture_render_result(scene):
    import numpy as np
    rr = bpy.data.images.get("Render Result")
    if rr is None:
        return None, "没有渲染结果 —— 先 F12 渲一张"
    tmp = os.path.join(tempfile.gettempdir(), "dlss5_render_in.png")
    ims = scene.render.image_settings
    saved = (ims.file_format, ims.color_depth, ims.color_mode)
    try:
        ims.file_format = "PNG"
        ims.color_depth = "16"
        ims.color_mode = "RGB"
        rr.save_render(tmp, scene=scene)
    except Exception as exc:                                # noqa: BLE001
        return None, "渲染结果保存失败: %s" % exc
    finally:
        try:
            ims.file_format, ims.color_depth, ims.color_mode = saved
        except Exception:                                   # noqa: BLE001
            pass
    if not os.path.exists(tmp):
        return None, "渲染结果为空 —— 先 F12 渲一张"
    rgb = _load_png_rgb(tmp)
    if rgb is None:
        return None, "渲染结果读回为空"
    return rgb, ""


# ==========================================================================
# 叠加层 + 上一次结果
# ==========================================================================
class _Overlay:
    tex = None
    size = (0, 0)
    handle = None
    region_ptr = None       # 只在抓取时的那个视口区域里画
    view_key = None
    dirty = False
    last_change = 0.0
    busy = False
    rect = None             # 叠加层覆盖的区域像素矩形 (x, y, w, h):相机视角 = 相机框
    dragging = False        # 正在拖分割线(画得更醒目)
    hover = False           # 鼠标靠近分割线


SPLIT_GRAB_PX = 14          # 离分割线多少像素内可以抓
VIEW_ITEMS = [
    ("SPLIT", "分割对比", "左原视口 / 右 NR,黄线可以直接在视口里拖"),
    ("NR", "只看 NR", "整个矩形都显示 NR 结果"),
    ("ORIG", "只看原图", "暂时隐藏叠加层,看原视口"),
]


class _Live:
    """实时模式,两条路,协议相同(hwnd / 视口矩形 / 参数 -> 状态):
    GPU 路:dlss5_live.exe(C++,DWM 窗口表面/按窗口捕获/桌面复制 -> D3D12 -> NGX -> DirectComposition 浮窗,全程不下 GPU,
           全分辨率 50-60 帧/秒);
    CPU 路:工作进程里的 nr_monitor.py(PrintWindow -> numpy -> NGX -> GDI 分层窗,约 20 帧/秒)。
    Blender 这边只负责:告诉它 窗口句柄 / 视口矩形 / 参数,定时拿状态。"""
    enabled = False
    route = "cpu"           # "gpu" / "cpu"
    area_ptr = None
    region_ptr = None
    hwnd = None
    last_cfg = None
    fps = 0.0
    nr_ms = 0.0
    total_ms = 0.0
    err = ""
    frames = 0
    size = (0, 0)
    visible = False
    capture = ""            # GPU 路实际用的抓取方式: dwm / wgc / dda


class _GpuLive:
    """dlss5_live.exe 的客户端:一行 JSON 进,一行 JSON 出。"""
    proc = None
    err = ""
    log_path = os.path.join(tempfile.gettempdir(), "dlss5_live_gpu.log")
    _log = None
    lock = threading.Lock()

    @classmethod
    def exe_path(cls, settings):
        cands = [bpy.path.abspath(settings.nr_live_exe) if settings.nr_live_exe else "",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "dlss5_live.exe")]
        for c in cands:
            if c and os.path.exists(c):
                return c
        return None

    @classmethod
    def alive(cls):
        return cls.proc is not None and cls.proc.poll() is None

    @classmethod
    def start(cls, exe, dll_dir):
        cls.stop()
        try:
            cls._log = open(cls.log_path, "wb")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            cls.proc = subprocess.Popen([exe, "--dll-dir", dll_dir],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=cls._log,
                                        creationflags=flags, text=True, encoding="utf-8", bufsize=1)
        except Exception as exc:                            # noqa: BLE001
            cls.err = "启动 dlss5_live.exe 失败: %s" % exc
            return False
        hello = cls._read(timeout=30.0)
        if not hello or not hello.get("ok"):
            cls.err = (hello or {}).get("err") or "dlss5_live.exe 无响应(见 %s)" % cls.log_path
            cls.stop()
            return False
        cls.err = ""
        return True

    @classmethod
    def _read(cls, timeout):
        box = []

        def rd():
            try:
                line = cls.proc.stdout.readline()
                box.append(json.loads(line) if line.strip() else None)
            except Exception:                               # noqa: BLE001
                box.append(None)
        t = threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout)
        return box[0] if box else None

    @classmethod
    def send(cls, obj, timeout=10.0):
        if not cls.alive():
            return None
        with cls.lock:
            try:
                cls.proc.stdin.write(json.dumps(obj) + "\n")
                cls.proc.stdin.flush()
            except Exception as exc:                        # noqa: BLE001
                cls.err = "写入失败: %s" % exc
                return None
            return cls._read(timeout)

    @classmethod
    def stop(cls):
        p = cls.proc
        cls.proc = None
        if p is not None:
            try:
                if p.poll() is None:
                    try:
                        p.stdin.write('{"cmd":"stop"}\n')
                        p.stdin.flush()
                    except Exception:                       # noqa: BLE001
                        pass
                    try:
                        p.wait(timeout=2.0)
                    except Exception:                       # noqa: BLE001
                        p.kill()
            except Exception:                               # noqa: BLE001
                pass
        if cls._log is not None:
            try:
                cls._log.close()
            except Exception:                               # noqa: BLE001
                pass
            cls._log = None


class _Last:
    kind = None             # "viewport" / "render"
    orig = None             # 全分辨率 float32 (h, w, 3)
    nr = None
    orig_small = None       # 视口:叠加层分辨率
    nr_small = None
    out = None              # 上次合成结果(全分辨率)
    scale = 1
    win = None
    area_ptr = None
    region_ptr = None
    capture_ms = 0.0
    nr_ms = 0.0
    size = (0, 0)


def _view_key(region, rv3d):
    try:
        vm = tuple(round(x, 4) for row in rv3d.view_matrix for x in row)
        wm = tuple(round(x, 4) for row in rv3d.window_matrix for x in row)
        return (vm, wm, region.width, region.height)
    except Exception:                                       # noqa: BLE001
        return None


def _camera_rect(region, rv3d, scene):
    """相机视角下相机框在区域里的像素矩形 (x, y, w, h);非相机视角返回 None。

    不夹到区域范围内:相机框放大到超出视口时,render.opengl 仍然渲的是整个相机框,
    叠加层也就要按真实框位置画(超出部分由 GPU 裁掉),否则对不齐。
    """
    try:
        if rv3d.view_perspective != "CAMERA" or scene.camera is None:
            return None
        from bpy_extras import view3d_utils
        import math
        cam = scene.camera
        xs, ys = [], []
        for v in cam.data.view_frame(scene=scene):
            p = view3d_utils.location_3d_to_region_2d(region, rv3d, cam.matrix_world @ v)
            if p is None:
                return None
            xs.append(p.x)
            ys.append(p.y)
        x0, x1 = int(math.floor(min(xs))), int(math.ceil(max(xs)))
        y0, y1 = int(math.floor(min(ys))), int(math.ceil(max(ys)))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return (x0, y0, x1 - x0, y1 - y0)
    except Exception:                                       # noqa: BLE001
        return None


def _blender_hwnd():
    """当前 Blender 主窗口的 Win32 句柄(在主线程里调用)。"""
    import ctypes
    user32 = ctypes.windll.user32
    user32.GetActiveWindow.restype = ctypes.c_void_p
    h = user32.GetActiveWindow()
    if h:
        return int(h)
    # 备用:按进程号找可见的 GHOST 窗口
    found = []
    pid = os.getpid()
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lp):
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(ctypes.c_void_p(hwnd), cls, 64)
            if "GHOST" in cls.value:
                found.append(hwnd)
        return True
    user32.EnumWindows(proto(cb), 0)
    return int(found[0]) if found else None


def _live_cfg(win, area, region, settings):
    """监视窗需要的配置:视口矩形(客户区坐标,顶行原点)+ 参数。"""
    rv3d = area.spaces.active.region_3d
    rx, ry, rw, rh = _clip_rect(_target_rect(region, rv3d, win.scene), region)
    left = region.x + rx
    top = win.height - (region.y + ry + rh)
    # 区域重叠时压在视口上的面板/工具栏/标题栏:让浮窗在那里透明,UI 保持清晰
    holes = []
    for r in area.regions:
        if r.type == "WINDOW" or r.width <= 1 or r.height <= 1:
            continue
        hx = r.x - left
        hy = (win.height - (r.y + r.height)) - top
        if hx + r.width <= 0 or hy + r.height <= 0 or hx >= rw or hy >= rh:
            continue
        holes.append([int(hx), int(hy), int(r.width), int(r.height)])
    return dict(hwnd=_Live.hwnd, rect=[int(left), int(top), int(rw), int(rh)], holes=holes,
                style=int(settings.nr_style), tone=float(settings.nr_tone),
                structure=float(settings.nr_structure), skin=float(settings.nr_skin),
                automask=bool(settings.nr_automask), mode=settings.nr_mode,
                strength=float(settings.nr_strength), half=bool(settings.nr_live_half),
                split=float(settings.nr_split), view=settings.nr_view,
                capture=settings.nr_live_capture.lower()), (rx, ry, rw, rh)


def _live_send(action, cfg=None):
    """给当前路线发一条控制消息,返回 (状态 dict 或 None, 错误)。"""
    if _Live.route == "gpu":
        req = {"cmd": action}
        if cfg is not None:
            req["cfg"] = cfg
        obj = _GpuLive.send(req)
        if obj is None:
            return None, _GpuLive.err or "dlss5_live.exe 已退出(见 %s)" % _GpuLive.log_path
        return obj, ""
    req = {"cmd": "monitor", "action": action}
    if cfg is not None:
        req["cfg"] = cfg
    return _Worker.exchange(req, b"", timeout=10.0)


def _live_tick():
    """每 0.1s:视口矩形 / 参数变了就推给实时进程;顺便拿状态刷新面板。"""
    if not _Live.enabled:
        return None
    win, area, region = _find_view(_Live.area_ptr, _Live.region_ptr)
    alive = _GpuLive.alive() if _Live.route == "gpu" else _Worker.alive()
    if region is None or not alive:
        _live_stop()
        return None
    try:
        cfg, rect_region = _live_cfg(win, area, region, win.scene.dlssnr)
        action = "update" if cfg != _Live.last_cfg else "status"
        obj, err = _live_send(action, cfg if action == "update" else None)
        if obj is None:
            _Live.err = err
            _live_stop()
            return None
        _Live.last_cfg = cfg
        _Overlay.rect = rect_region
        _Live.fps = float(obj.get("fps", 0.0))
        _Live.total_ms = float(obj.get("ms", 0.0))
        _Live.nr_ms = float(obj.get("nr_ms", 0.0))
        _Live.frames = int(obj.get("frames", 0))
        _Live.size = tuple(obj.get("size", (0, 0)))
        _Live.visible = bool(obj.get("visible", False))
        _Live.capture = obj.get("capture", "") or ""
        _Live.err = obj.get("err", "") or ""
        if not obj.get("running", True):
            _live_stop()
            return None
        for r in area.regions:
            if r.type == "UI":
                r.tag_redraw()
    except Exception as exc:                                # noqa: BLE001
        _Live.err = str(exc)
    return 0.1


def _live_start(context):
    settings = context.scene.dlssnr
    hwnd = _blender_hwnd()
    if not hwnd:
        return False, "找不到 Blender 窗口句柄"
    root = bpy.path.abspath(settings.nr_root)
    exe = _GpuLive.exe_path(settings)
    route = settings.nr_route
    if route == "AUTO":
        route = "GPU" if exe else "CPU"
    if route == "GPU":
        if not exe:
            return False, "找不到 dlss5_live.exe(GPU 路),在「高级」里指定或切到 CPU 路"
        dll_dir = os.path.join(root, "runtime")
        if not os.path.exists(os.path.join(dll_dir, "nvngx_dlssnr.dll")):
            return False, "runtime 目录里没有 nvngx_dlssnr.dll: %s" % dll_dir
        if not _GpuLive.start(exe, dll_dir):
            return False, _GpuLive.err
        _Live.route = "gpu"
    else:
        if not _Worker.ensure(root):
            return False, _Worker.err
        _Live.route = "cpu"
    _Live.hwnd = hwnd
    _Live.area_ptr = context.area.as_pointer()
    _Live.region_ptr = context.region.as_pointer()
    _Live.fps = 0.0
    _Live.frames = 0
    _Live.err = ""
    cfg, rect_region = _live_cfg(context.window, context.area, context.region, settings)
    _Live.enabled = True             # _live_send 看这个决定走哪条路
    obj, err = _live_send("start", cfg)
    if obj is None or not obj.get("ok", 1):
        _Live.enabled = False
        if _Live.route == "gpu":
            _GpuLive.stop()
        return False, err or (obj or {}).get("err", "启动失败")
    _Live.last_cfg = cfg
    # 视口里的黄线拖动/命中判定复用叠加层的矩形;叠加层纹理本身不画(浮窗在画)
    _Overlay.tex = None
    _Overlay.rect = rect_region
    _Overlay.region_ptr = _Live.region_ptr
    settings.nr_live = True
    if not bpy.app.timers.is_registered(_live_tick):
        bpy.app.timers.register(_live_tick, first_interval=0.1, persistent=True)
    return True, ""


def _live_stop():
    if not _Live.enabled:
        return
    _Live.enabled = False
    try:
        if _Live.route == "gpu":
            _GpuLive.stop()
        elif _Worker.alive():
            _Worker.exchange({"cmd": "monitor", "action": "stop"}, b"", timeout=5.0)
    except Exception:                                       # noqa: BLE001
        pass
    try:
        for sc in bpy.data.scenes:
            sc.dlssnr.nr_live = False
    except Exception:                                       # noqa: BLE001
        pass
    try:
        if bpy.app.timers.is_registered(_live_tick):
            bpy.app.timers.unregister(_live_tick)
    except Exception:                                       # noqa: BLE001
        pass


def _overlay_draw():
    try:
        region = bpy.context.region
        rv3d = bpy.context.region_data
        if region is None or rv3d is None or region.type != "WINDOW":
            return
        if _Live.enabled or _Overlay.tex is None:
            return                         # 实时模式由浮窗显示,这里不画
        key = _view_key(region, rv3d)
        if _Overlay.region_ptr is not None and region.as_pointer() != _Overlay.region_ptr:
            return
        if _Overlay.view_key is not None and key is not None and key != _Overlay.view_key:
            if not _Overlay.dirty:
                _Overlay.dirty = True
                _Overlay.last_change = time.time()
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        try:
            s = bpy.context.scene.dlssnr
            split, view = float(s.nr_split), s.nr_view
        except Exception:                                   # noqa: BLE001
            split, view = 0.5, "SPLIT"
        if view == "ORIG":
            return
        if view == "NR":
            split = 0.0
        rect = _Overlay.rect or (0, 0, region.width, region.height)
        rx, ry, rw, rh = rect
        x0 = rx + int(round(rw * split))
        x1, y1 = rx + rw, ry + rh
        if x0 < x1:
            u0 = (x0 - rx) / float(rw)
            sh = gpu.shader.from_builtin("IMAGE")
            batch = batch_for_shader(
                sh, "TRI_FAN",
                {"pos": [(x0, ry), (x1, ry), (x1, y1), (x0, y1)],
                 "texCoord": [(u0, 0), (1, 0), (1, 1), (u0, 1)]})
            gpu.state.blend_set("NONE")
            sh.bind()
            sh.uniform_sampler("image", _Overlay.tex)
            batch.draw(sh)
        if view == "SPLIT":
            _draw_split_line(x0, ry, y1, region)
    except Exception:                                       # noqa: BLE001
        pass


def _draw_split_line(x0, y0, y1, region):
    """分割线 + 上下两个抓手;拖动/悬停时更粗更亮。"""
    import gpu
    from gpu_extras.batch import batch_for_shader
    hot = _Overlay.dragging or _Overlay.hover
    half = 2 if hot else 1
    color = (1.0, 0.95, 0.4, 1.0) if hot else (1.0, 0.8, 0.15, 0.9)
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    sh.bind()
    sh.uniform_float("color", color)
    y0c, y1c = max(y0, 0), min(y1, region.height)
    batch_for_shader(sh, "TRI_FAN", {"pos": [(x0 - half, y0c), (x0 + half, y0c),
                                             (x0 + half, y1c), (x0 - half, y1c)]}).draw(sh)
    # 抓手:上下各一个小三角,提示"这里能拖"
    g = 9 if hot else 7
    for yy, d in ((y1c, -1), (y0c, 1)):
        batch_for_shader(sh, "TRIS", {"pos": [(x0 - g, yy), (x0 + g, yy), (x0, yy + d * g)]}).draw(sh)
    gpu.state.blend_set("NONE")


def _find_view(area_ptr, region_ptr):
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type != "VIEW_3D" or area.as_pointer() != area_ptr:
                continue
            for region in area.regions:
                if region.type == "WINDOW" and region.as_pointer() == region_ptr:
                    return win, area, region
    return None, None, None


def _auto_tick():
    """视角稳定 0.35s 后自动重截。app 定时器,无模态。"""
    try:
        scene = bpy.context.scene
        auto = scene.dlssnr.nr_auto if (scene and hasattr(scene, "dlssnr")) else False
    except Exception:                                       # noqa: BLE001
        auto = False
    if not auto or _Overlay.tex is None or _Last.kind != "viewport" or _Live.enabled:
        return 0.3
    if _Overlay.busy or not _Overlay.dirty:
        return 0.3
    if time.time() - _Overlay.last_change < 0.35:
        return 0.15
    _Overlay.busy = True
    try:
        win, area, region = _find_view(_Last.area_ptr, _Last.region_ptr)
        if area is not None:
            ok, _err = _nr_viewport(win.scene.dlssnr, win, area, region)
            if ok:
                _Overlay.dirty = False
            area.tag_redraw()
        else:
            _Overlay.tex = None
    except Exception:                                       # noqa: BLE001
        pass
    finally:
        _Overlay.busy = False
    return 0.3


# ==========================================================================
# NR 流水线
# ==========================================================================
def _nr_params(settings):
    return dict(style=int(settings.nr_style), preset=0,
                tone=float(settings.nr_tone), structure=float(settings.nr_structure),
                skin=float(settings.nr_skin), automask=bool(settings.nr_automask),
                iters=int(settings.nr_iters), temporal=1)


def _run_nr(settings, rgb):
    if not _Worker.ensure(bpy.path.abspath(settings.nr_root)):
        return None, _Worker.err
    t0 = time.perf_counter()
    nr, err = _Worker.process(rgb, _nr_params(settings))
    _Last.nr_ms = (time.perf_counter() - t0) * 1000.0
    if nr is None:
        return None, err
    return nr, ""


def _pick_scale(settings, region):
    if settings.nr_scale == "1":
        return 1
    if settings.nr_scale == "2":
        return 2
    return 2 if region.height < 900 else 1


def _ensure_worker_started(settings):
    return _Worker.ensure(bpy.path.abspath(settings.nr_root))


def _push_overlay(rgb_small, region):
    import gpu
    h, w = rgb_small.shape[:2]
    import numpy as np
    rgba = np.concatenate([rgb_small[::-1], np.ones((h, w, 1), np.float32)], axis=2)
    data = array.array("f", np.ascontiguousarray(rgba, dtype=np.float32).tobytes())
    _Overlay.tex = gpu.types.GPUTexture((w, h), format="RGBA16F",
                                        data=gpu.types.Buffer("FLOAT", w * h * 4, data))
    _Overlay.size = (w, h)


def _nr_viewport(settings, win, area, region):
    """抓当前视口 -> NR -> 合成 -> 叠加层。返回 (ok, err)。"""
    space = area.spaces.active
    if space.shading.type == "RENDERED" and win.scene.render.engine == "CYCLES":
        return False, "Cycles 渲染模式的视口抓取会很慢,切到材质预览,或 F12 后用「NR 上一帧渲染」"
    scale = _pick_scale(settings, region)
    t0 = time.perf_counter()
    try:
        rgb, rect, scale = _capture_viewport(win, area, region, scale)
    except Exception as exc:                                # noqa: BLE001
        return False, "视口抓取失败: %s" % exc
    _Last.capture_ms = (time.perf_counter() - t0) * 1000.0
    nr, err = _run_nr(settings, rgb)
    if nr is None:
        return False, err
    _Last.kind = "viewport"
    _Last.orig, _Last.nr, _Last.scale = rgb, nr, scale
    _Last.orig_small = _downsample(rgb, scale)
    _Last.nr_small = _downsample(nr, scale)
    _Last.out = None
    _Last.size = (rgb.shape[1], rgb.shape[0])
    _Last.win = win
    _Last.area_ptr, _Last.region_ptr = area.as_pointer(), region.as_pointer()
    out_small = _composite(_Last.orig_small, _Last.nr_small, settings.nr_mode, settings.nr_strength)
    _push_overlay(out_small, region)
    rv3d = space.region_3d
    _Overlay.region_ptr = region.as_pointer()
    _Overlay.view_key = _view_key(region, rv3d)
    _Overlay.rect = rect
    _Overlay.dirty = False
    settings.nr_last_info = "%dx%d  抓取 %.0fms  NR %.0fms" % (
        rgb.shape[1], rgb.shape[0], _Last.capture_ms, _Last.nr_ms)
    return True, ""


def _recomposite(context):
    """合成方式 / 强度变了:不重跑模型,只重新合成。"""
    settings = context.scene.dlssnr
    if _Last.kind == "viewport" and _Last.orig_small is not None:
        out_small = _composite(_Last.orig_small, _Last.nr_small, settings.nr_mode, settings.nr_strength)
        win, area, region = _find_view(_Last.area_ptr, _Last.region_ptr)
        if region is not None:
            _push_overlay(out_small, region)
            area.tag_redraw()
        _Last.out = None
    elif _Last.kind == "render" and _Last.orig is not None:
        _Last.out = _composite(_Last.orig, _Last.nr, settings.nr_mode, settings.nr_strength)
        _to_image(NR_IMAGE, _Last.out)
        for area in context.screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.tag_redraw()


def _full_result(settings):
    if _Last.out is None and _Last.orig is not None:
        _Last.out = _composite(_Last.orig, _Last.nr, settings.nr_mode, settings.nr_strength)
    return _Last.out


# ==========================================================================
# 运算符:神经渲染
# ==========================================================================
class DLSSNR_OT_live_toggle(Operator):
    """实时模式:视口每次重画都直读帧缓冲送去 NR,结果回来自动贴上(开/关)"""
    bl_idname = "dlssnr.live_toggle"
    bl_label = "实时模式"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if _Live.enabled:
            _live_stop()
            for a in context.screen.areas:
                if a.type == "VIEW_3D":
                    a.tag_redraw()
            self.report({"INFO"}, "实时模式已关(共 %d 帧)" % _Live.frames)
            return {"FINISHED"}
        if context.area is None or context.area.type != "VIEW_3D" or context.region is None:
            self.report({"ERROR"}, "请在 3D 视口里使用")
            return {"CANCELLED"}
        ok, err = _live_start(context)
        if not ok:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        self.report({"INFO"}, "实时模式已开:NR 浮窗盖在视口上,鼠标操作照常穿透;再点一次关闭")
        return {"FINISHED"}


class DLSSNR_OT_nr_once(Operator):
    """抓取当前视口 -> DLSS 5 神经渲染 -> 叠加显示(左原图 / 右 NR)"""
    bl_idname = "dlssnr.nr_once"
    bl_label = "NR 当前视口"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.dlssnr
        if context.area is None or context.area.type != "VIEW_3D":
            self.report({"ERROR"}, "请在 3D 视口里使用")
            return {"CANCELLED"}
        if _Live.enabled:
            _live_stop()
        region = next((r for r in context.area.regions if r.type == "WINDOW"), None)
        _Overlay.busy = True
        try:
            ok, err = _nr_viewport(settings, context.window, context.area, region)
        finally:
            _Overlay.busy = False
        if not ok:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        context.area.tag_redraw()
        self.report({"INFO"}, "NR 完成:%s。拖动分割线对比;视角变了会自动刷新" % settings.nr_last_info)
        return {"FINISHED"}


def _split_hit(context, event):
    """鼠标是否落在分割线的抓取范围内。返回 (rect, x_line) 或 None。"""
    try:
        if (_Overlay.tex is None and not _Live.enabled) or context.scene.dlssnr.nr_view != "SPLIT":
            return None
        region = context.region
        if region is None or region.type != "WINDOW" or region.as_pointer() != _Overlay.region_ptr:
            return None
        if _Overlay.dirty and not _Live.enabled:
            return None
        rect = _Overlay.rect or (0, 0, region.width, region.height)
        rx, ry, rw, rh = rect
        x_line = rx + rw * float(context.scene.dlssnr.nr_split)
        mx, my = event.mouse_region_x, event.mouse_region_y
        if abs(mx - x_line) > SPLIT_GRAB_PX:
            return None
        if my < max(ry, 0) - SPLIT_GRAB_PX or my > min(ry + rh, region.height) + SPLIT_GRAB_PX:
            return None
        return rect, x_line
    except Exception:                                       # noqa: BLE001
        return None


class DLSSNR_OT_drag_split(Operator):
    """在视口里直接拖动对比分割线(鼠标靠近黄线按住左键拖)"""
    bl_idname = "dlssnr.drag_split"
    bl_label = "拖动对比分割线"
    bl_options = {"INTERNAL"}          # 不进 Info 日志、不进撤销栈

    _rect = None
    _start_split = 0.5

    def invoke(self, context, event):
        hit = _split_hit(context, event)
        if hit is None:
            return {"PASS_THROUGH"}
        self._rect = hit[0]
        self._start_split = context.scene.dlssnr.nr_split
        _Overlay.dragging = True
        try:
            context.window.cursor_modal_set("MOVE_X")
        except Exception:                                   # noqa: BLE001
            pass
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _apply(self, context, event):
        rx, _ry, rw, _rh = self._rect
        split = (event.mouse_region_x - rx) / float(max(rw, 1))
        context.scene.dlssnr.nr_split = min(1.0, max(0.0, split))

    def _end(self, context):
        _Overlay.dragging = False
        try:
            context.window.cursor_modal_restore()
        except Exception:                                   # noqa: BLE001
            pass
        if context.area is not None:
            context.area.tag_redraw()

    def modal(self, context, event):
        if event.type == "MOUSEMOVE":
            self._apply(context, event)
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self._apply(context, event)
            self._end(context)
            return {"FINISHED"}
        if event.type in {"ESC", "RIGHTMOUSE"}:
            context.scene.dlssnr.nr_split = self._start_split
            self._end(context)
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}


class DLSSNR_OT_split_hover(Operator):
    """鼠标靠近分割线时高亮(只改一个标志位,立刻放行事件)"""
    bl_idname = "dlssnr.split_hover"
    bl_label = "分割线悬停"
    bl_options = {"INTERNAL"}          # 每次鼠标移动都会触发,绝不能进 Info 日志

    def invoke(self, context, event):
        if _Overlay.tex is None and not _Live.enabled:
            return {"PASS_THROUGH"}
        hot = _split_hit(context, event) is not None
        if hot != _Overlay.hover:
            _Overlay.hover = hot
            if context.area is not None:
                context.area.tag_redraw()
        return {"PASS_THROUGH"}


class DLSSNR_OT_cycle_view(Operator):
    """在 分割对比 / 只看 NR / 只看原图 之间切换"""
    bl_idname = "dlssnr.cycle_view"
    bl_label = "切换对比方式"

    def execute(self, context):
        s = context.scene.dlssnr
        order = [it[0] for it in VIEW_ITEMS]
        s.nr_view = order[(order.index(s.nr_view) + 1) % len(order)]
        for a in context.screen.areas:
            if a.type == "VIEW_3D":
                a.tag_redraw()
        return {"FINISHED"}


class DLSSNR_OT_clear_overlay(Operator):
    """清除视口上的 NR 叠加层"""
    bl_idname = "dlssnr.clear_overlay"
    bl_label = "清除叠加"

    def execute(self, context):
        if _Live.enabled:
            _live_stop()
        _Overlay.tex = None
        _Overlay.view_key = None
        _Overlay.region_ptr = None
        _Overlay.dirty = False
        for a in context.screen.areas:
            if a.type == "VIEW_3D":
                a.tag_redraw()
        return {"FINISHED"}


class DLSSNR_OT_nr_render(Operator):
    """对最近一次 F12 渲染结果(Cycles / EEVEE 都行)做 DLSS 5 神经渲染,写进图像「DLSS5_NR」"""
    bl_idname = "dlssnr.nr_render"
    bl_label = "NR 上一帧渲染"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.dlssnr
        t0 = time.perf_counter()
        rgb, err = _capture_render_result(context.scene)
        if rgb is None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        _Last.capture_ms = (time.perf_counter() - t0) * 1000.0
        nr, err = _run_nr(settings, rgb)
        if nr is None:
            self.report({"ERROR"}, "NR 失败: %s" % err)
            return {"CANCELLED"}
        _Last.kind = "render"
        _Last.orig, _Last.nr, _Last.scale = rgb, nr, 1
        _Last.orig_small = _Last.nr_small = None
        # 视口叠加层对应的是上一次视口结果,现在「上一次结果」换成渲染了,清掉免得混淆
        _Overlay.tex = None
        _Overlay.view_key = None
        _Overlay.region_ptr = None
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
        _Last.size = (rgb.shape[1], rgb.shape[0])
        _Last.out = _composite(rgb, nr, settings.nr_mode, settings.nr_strength)
        _to_image(NR_IMAGE_ORIG, rgb)
        img = _to_image(NR_IMAGE, _Last.out)
        shown = False
        for area in context.screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = img
                area.tag_redraw()
                shown = True
        settings.nr_last_info = "%dx%d  读回 %.0fms  NR %.0fms" % (
            rgb.shape[1], rgb.shape[0], _Last.capture_ms, _Last.nr_ms)
        self.report({"INFO"}, "NR 完成:%s -> 图像「%s」%s" % (
            settings.nr_last_info, NR_IMAGE, "" if shown else "(在图像编辑器里选它)"))
        return {"FINISHED"}


class DLSSNR_OT_save_png(Operator):
    """把上一次 NR 结果(全分辨率)另存为 PNG,并写进图像「DLSS5_NR」"""
    bl_idname = "dlssnr.save_png"
    bl_label = "另存 PNG"

    def execute(self, context):
        settings = context.scene.dlssnr
        if _Last.orig is None:
            self.report({"ERROR"}, "还没有 NR 结果")
            return {"CANCELLED"}
        out = _full_result(settings)
        outdir = bpy.path.abspath(settings.save_dir)
        if not outdir:
            self.report({"ERROR"}, "请先设置保存目录")
            return {"CANCELLED"}
        try:
            os.makedirs(outdir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            tag = "viewport" if _Last.kind == "viewport" else "render"
            p_out = os.path.join(outdir, "nr_%s_%s.png" % (tag, stamp))
            p_in = os.path.join(outdir, "nr_%s_%s_原图.png" % (tag, stamp))
            _write_png(p_out, out)
            _write_png(p_in, _Last.orig)
            _to_image(NR_IMAGE, out)
            _to_image(NR_IMAGE_ORIG, _Last.orig)
        except Exception as exc:                            # noqa: BLE001
            self.report({"ERROR"}, "保存失败: %s" % exc)
            return {"CANCELLED"}
        self.report({"INFO"}, "已保存 %s(原图在旁边)" % p_out)
        return {"FINISHED"}


class DLSSNR_OT_worker_restart(Operator):
    """重启 NR 工作进程(卡住 / 换了运行时目录时用)"""
    bl_idname = "dlssnr.worker_restart"
    bl_label = "重启工作进程"

    def execute(self, context):
        settings = context.scene.dlssnr
        _Worker.stop()
        _Worker.state = "off"
        if _Worker.ensure(bpy.path.abspath(settings.nr_root)):
            self.report({"INFO"}, "工作进程就绪:%s / 桥 %s" % (
                _Worker.info.get("gpu", "?"), _Worker.info.get("version", "?")))
            return {"FINISHED"}
        self.report({"ERROR"}, _Worker.err)
        return {"CANCELLED"}


class DLSSNR_OT_worker_stop(Operator):
    """停止 NR 工作进程,释放显存"""
    bl_idname = "dlssnr.worker_stop"
    bl_label = "停止工作进程"

    def execute(self, context):
        _Worker.stop()
        _Worker.state = "off"
        self.report({"INFO"}, "工作进程已停止")
        return {"FINISHED"}


# ==========================================================================
# 合成器节点图(引导通道)—— 未改动
# ==========================================================================
def _apply_depth(half):
    ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
    if ng is None:
        return
    fo = next((n for n in ng.nodes if n.bl_idname == "CompositorNodeOutputFile"), None)
    if fo is None:
        return
    try:
        fo.format.color_depth = "16" if half else "32"
    except Exception:                                       # noqa: BLE001
        pass


def _get_or_build_group():
    ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
    if ng is None:
        ng = bpy.data.node_groups.new(NODE_GROUP_NAME, "CompositorNodeTree")
    tree = ng
    for n in list(tree.nodes):
        tree.nodes.remove(n)
    rl = tree.nodes.new("CompositorNodeRLayers")
    rl.location = (0, 0)
    fo = tree.nodes.new("CompositorNodeOutputFile")
    fo.location = (460, -320)
    fo.format.file_format = "OPEN_EXR_MULTILAYER"
    fo.format.compression = 3
    fo.format.color_depth = "32"
    vi = tree.nodes.new("CompositorNodeViewer")
    vi.location = (460, 220)
    wired = []
    for out in rl.outputs:
        if out.name not in GUIDE_SOCKETS:
            continue
        slot_type = "FLOAT" if out.bl_idname == "NodeSocketFloat" else "RGBA"
        fo.file_output_items.new(slot_type, out.name)
        tree.links.new(out, fo.inputs[out.name])
        wired.append(out.name)
    return ng, rl, fo, vi, wired, [o.name for o in rl.outputs]


def _enable_guide_passes(view_layer):
    wanted = ("use_pass_combined", "use_pass_z", "use_pass_normal", "use_pass_vector")
    for p in wanted:
        if hasattr(view_layer, p):
            setattr(view_layer, p, True)
    return [p for p in wanted if hasattr(view_layer, p)]


def _wire_viewer(ng, rl, vi, guide):
    if vi is None or rl is None:
        return False
    for lk in list(ng.links):
        if lk.to_node == vi:
            ng.links.remove(lk)
    if guide not in [o.name for o in rl.outputs]:
        return False
    ng.links.new(rl.outputs[guide], vi.inputs["Image"])
    return True


def _prepare_scene(scene, outdir=None):
    view_layer = bpy.context.view_layer
    enabled = _enable_guide_passes(view_layer)
    scene.use_nodes = True
    scene.render.use_compositing = True
    ng, rl, fo, vi, wired, all_outputs = _get_or_build_group()
    if outdir:
        fo.directory = outdir
    half = False
    if hasattr(scene, "dlssnr"):
        half = scene.dlssnr.half_float
    _apply_depth(half)
    scene.compositing_node_group = ng
    return dict(passes_enabled=enabled, guide_outputs_available=all_outputs,
                layers_wired=wired, node_group=ng.name, rl=rl, vi=vi, ng=ng, fo=fo)


def _snapshot(directory):
    seen = {}
    if os.path.isdir(directory):
        for fn in os.listdir(directory):
            p = os.path.join(directory, fn)
            if os.path.isfile(p):
                seen[p] = os.path.getsize(p)
    return seen


def _collect_new(directory, before):
    out = []
    after = _snapshot(directory)
    for p, size in sorted(after.items()):
        old = before.get(p)
        if old is None or size > old:
            out.append((p, size))
    return out


def _read_exr_parts(path):
    with open(path, "rb") as f:
        magic, _ver = struct.unpack("<II", f.read(8))
        if magic != 0x01312F76:
            return None

        def cstr():
            b = b""
            while True:
                c = f.read(1)
                if not c:
                    raise EOFError("EXR 头部意外结束")
                if c == b"\x00":
                    return b.decode("ascii", "replace")
                b += c

        pt = {0: "UINT", 1: "HALF", 2: "FLOAT"}
        parts = []
        while True:
            attrs = {}
            while True:
                aname = cstr()
                if aname == "":
                    break
                _atype = cstr()
                (asize,) = struct.unpack("<I", f.read(4))
                attrs[aname] = f.read(asize)
            if "channels" not in attrs:
                break
            chans, pos = [], 0
            blob = attrs["channels"]
            while pos < len(blob):
                end = blob.index(b"\x00", pos)
                if end == pos:
                    break
                cname = blob[pos:end].decode("ascii", "replace")
                pos = end + 1
                ctype = struct.unpack("<i", blob[pos:pos + 4])[0]
                pos += 16
                chans.append((cname, pt.get(ctype, "?")))
            parts.append({
                "name": attrs.get("name", b"").decode("ascii", "replace").strip("\x00"),
                "channels": chans,
                "compression": attrs.get("compression", b"\xff")[0],
            })
            probe = f.read(1)
            if not probe:
                break
            f.seek(-1, 1)
    return parts


class _LiveBase:
    _timer = None
    _fps_times = None
    _frames = 0
    _fails = 0
    _pct = 0
    _saved_pct = None
    _saved_path = None
    _saved_fmt = None
    _saved_comp = None
    _saved_use_nodes = None
    _last_ms = 0.0
    TARGET_MS = 220.0
    MIN_PCT = 8
    MAX_FAILS = 3

    def _adapt(self, context):
        try:
            if not context.scene.dlssnr.live_adaptive:
                return
            old = self._pct
            if self._last_ms > self.TARGET_MS * 1.6:
                self._pct = max(self.MIN_PCT, int(self._pct * 0.7))
            elif self._last_ms < self.TARGET_MS * 0.5:
                ceiling = self._saved_pct if self._saved_pct else 100
                self._pct = min(ceiling, int(self._pct * 1.25))
            if self._pct != old:
                context.scene.render.resolution_percentage = self._pct
        except Exception:                                   # noqa: BLE001
            pass

    def _save_render_settings(self, scene):
        self._saved_pct = scene.render.resolution_percentage
        self._saved_path = scene.render.filepath
        self._saved_fmt = scene.render.image_settings.file_format

    def _restore(self, context):
        scene = context.scene
        if self._saved_pct is not None:
            try:
                scene.render.resolution_percentage = self._saved_pct
            except Exception:                               # noqa: BLE001
                pass
            self._saved_pct = None
        if self._saved_path is not None:
            try:
                scene.render.filepath = self._saved_path
            except Exception:                               # noqa: BLE001
                pass
            self._saved_path = None
        if self._saved_fmt is not None:
            try:
                scene.render.image_settings.file_format = self._saved_fmt
            except Exception:                               # noqa: BLE001
                pass
            self._saved_fmt = None
        if self._saved_comp is not None or self._saved_use_nodes is not None:
            try:
                scene.compositing_node_group = self._saved_comp
            except Exception:                               # noqa: BLE001
                pass
            try:
                scene.use_nodes = bool(self._saved_use_nodes)
            except Exception:                               # noqa: BLE001
                pass
            self._saved_comp = None
            self._saved_use_nodes = None

    def _fps_tick(self, settings):
        now = time.perf_counter()
        if self._fps_times is None:
            self._fps_times = []
        self._fps_times.append(now)
        self._fps_times = [t for t in self._fps_times if now - t < 2.0]
        if len(self._fps_times) > 1:
            span = self._fps_times[-1] - self._fps_times[0]
            settings.live_fps = (len(self._fps_times) - 1) / span if span > 0 else 0.0

    def _redraw(self, context):
        try:
            for area in context.screen.areas:
                if area.type in {"VIEW_3D", "IMAGE_EDITOR"}:
                    area.tag_redraw()
        except Exception:                                   # noqa: BLE001
            pass

    def _finish(self, context):
        settings = context.scene.dlssnr
        settings.live_enabled = False
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:                               # noqa: BLE001
                pass
            self._timer = None
        self._restore(context)
        self.report({"INFO"}, "预览已停止(共渲染 %d 帧,设置已还原)" % self._frames)


class DLSSNR_OT_live_preview(_LiveBase, Operator):
    """持续重渲染,实时显示选中的引导通道(渲染在主线程,会卡界面,分辨率自动降)"""
    bl_idname = "dlssnr.live_preview"
    bl_label = "实时预览(引导通道)"
    bl_options = {"REGISTER"}

    def _render_once(self):
        t0 = time.perf_counter()
        try:
            bpy.ops.render.render(write_still=False)
        except Exception:                                   # noqa: BLE001
            return False
        self._last_ms = (time.perf_counter() - t0) * 1000.0
        return True

    def modal(self, context, event):
        settings = context.scene.dlssnr
        if event.type in {"ESC", "RIGHTMOUSE"} or not settings.live_enabled:
            self._finish(context)
            return {"CANCELLED"}
        if event.type == "TIMER":
            ok = self._render_once()
            if not ok:
                self._fails += 1
                if self._fails >= self.MAX_FAILS:
                    self.report({"ERROR"}, "连续渲染失败,实时预览已停止")
                    self._finish(context)
                    return {"CANCELLED"}
            else:
                self._fails = 0
                self._frames += 1
                self._adapt(context)
            settings.live_pct = self._pct
            settings.live_ms = self._last_ms
            self._fps_tick(settings)
            self._redraw(context)
        return {"PASS_THROUGH"}

    def invoke(self, context, event):
        settings = context.scene.dlssnr
        scene = context.scene
        if scene.render.engine != "BLENDER_EEVEE":
            self.report({"WARNING"}, "引导通道是 EEVEE 的功能,当前引擎不是 EEVEE")
        self._saved_comp = scene.compositing_node_group
        self._saved_use_nodes = scene.use_nodes
        report = _prepare_scene(scene)
        _wire_viewer(report["ng"], report["rl"], report["vi"], settings.guide)
        self._save_render_settings(scene)
        self._pct = max(self.MIN_PCT, min(self._saved_pct, int(settings.live_max_pct)))
        scene.render.resolution_percentage = self._pct
        self._frames = 0
        self._fails = 0
        self._last_ms = 0.0
        self._fps_times = None
        settings.live_enabled = True
        settings.live_fps = 0.0
        settings.live_pct = self._pct
        settings.live_ms = 0.0
        self._timer = context.window_manager.event_timer_add(
            max(0.05, settings.live_interval), window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, "实时预览已启动 — 分辨率降到 %d%%(退出时自动还原)" % self._pct)
        return {"RUNNING_MODAL"}


class DLSSNR_OT_stop_live(Operator):
    """停止预览"""
    bl_idname = "dlssnr.stop_live"
    bl_label = "停止预览"

    def execute(self, context):
        context.scene.dlssnr.live_enabled = False
        return {"FINISHED"}


class DLSSNR_OT_capture_guides(Operator):
    """渲染并把 EEVEE 的深度 / 法线 / 运动矢量引导通道写成多层 EXR"""
    bl_idname = "dlssnr.capture_guides"
    bl_label = "捕获引导通道"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.dlssnr
        outdir = bpy.path.abspath(settings.output_dir)
        if not outdir:
            self.report({"ERROR"}, "请先设置输出目录")
            return {"CANCELLED"}
        os.makedirs(outdir, exist_ok=True)
        scene = context.scene
        saved_comp = scene.compositing_node_group
        saved_use_nodes = scene.use_nodes
        report = _prepare_scene(scene, outdir)
        before = _snapshot(outdir)
        t0 = time.time()
        try:
            if settings.capture_animation:
                bpy.ops.render.render(animation=True)
            else:
                bpy.ops.render.render(write_still=True)
        except Exception as exc:                            # noqa: BLE001
            self.report({"ERROR"}, "渲染失败: %s" % exc)
            return {"CANCELLED"}
        finally:
            try:
                scene.compositing_node_group = saved_comp
                scene.use_nodes = saved_use_nodes
            except Exception:                               # noqa: BLE001
                pass
        elapsed = time.time() - t0
        produced = _collect_new(outdir, before)
        m = ["EEVEE 引导通道捕获报告",
             "blender   : %s" % bpy.app.version_string,
             "引擎      : %s" % scene.render.engine,
             "分辨率    : %dx%d @ %d%%" % (scene.render.resolution_x, scene.render.resolution_y,
                                          scene.render.resolution_percentage)]
        if settings.capture_animation:
            m.append("帧范围    : %d..%d" % (scene.frame_start, scene.frame_end))
        else:
            m.append("帧        : %d" % scene.frame_current)
        m.append("已开通道  : %s" % ", ".join(report["passes_enabled"]))
        m.append("渲染层输出: %s" % ", ".join(report["guide_outputs_available"]))
        m.append("已接线    : %s" % ", ".join(report["layers_wired"]))
        m.append("耗时      : %.2f 秒" % elapsed)
        m.append("")
        m.append("文件:")
        for path, size in produced:
            rel = os.path.relpath(path, outdir)
            parts = _read_exr_parts(path) if path.lower().endswith(".exr") else None
            if parts:
                desc = "; ".join("%s(%s)" % (p["name"] or "?", ",".join(c for c, _t in p["channels"]))
                                 for p in parts)
                m.append("  %-46s %12d B  parts=%d  %s" % (rel, size, len(parts), desc))
            else:
                m.append("  %-46s %12d B" % (rel, size))
        with open(os.path.join(outdir, MANIFEST_NAME), "w", encoding="utf-8") as fh:
            fh.write("\n".join(m))
        self.report({"INFO"}, "已捕获 %d 个文件,耗时 %.1f 秒" % (len(produced), elapsed))
        return {"FINISHED"}


# ==========================================================================
# 属性
# ==========================================================================
def _on_guide_change(self, context):
    try:
        ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
        if ng is None:
            return
        rl = next((n for n in ng.nodes if n.bl_idname == "CompositorNodeRLayers"), None)
        vi = next((n for n in ng.nodes if n.bl_idname == "CompositorNodeViewer"), None)
        if rl and vi:
            _wire_viewer(ng, rl, vi, self.guide)
    except Exception:                                       # noqa: BLE001
        pass


def _on_depth_change(self, context):
    _apply_depth(self.half_float)


def _on_composite_change(self, context):
    try:
        if _Live.enabled:
            return                          # 定时器会把新参数推给监视窗
        _recomposite(context)
    except Exception:                                       # noqa: BLE001
        pass


def _on_model_param_change(self, context):
    pass                                    # 实时:定时器推送;单帧:下次点 NR 生效


def _on_preset_change(self, context):
    p = next((p for p in PRESETS if p[0] == self.nr_preset), None)
    if p is None or p[3] is None:
        return
    _style, tone, struct, skin, mode, k = p[3], p[4], p[5], p[6], p[7], p[8]
    self.nr_style = str(_style)
    self.nr_tone, self.nr_structure, self.nr_skin = tone, struct, skin
    self.nr_mode, self.nr_strength = mode, k
    # 非实时:模型参数变了得重跑一次;有上一次抓图就直接重跑
    if not _Live.enabled and _Last.kind == "viewport":
        try:
            win, area, region = _find_view(_Last.area_ptr, _Last.region_ptr)
            if area is not None:
                _nr_viewport(self, win, area, region)
                area.tag_redraw()
        except Exception:                                   # noqa: BLE001
            pass


def _on_split_change(self, context):
    try:
        for a in context.screen.areas:
            if a.type == "VIEW_3D":
                a.tag_redraw()
    except Exception:                                       # noqa: BLE001
        pass


class DLSSNR_Settings(PropertyGroup):
    # ---- 神经渲染 ----
    nr_root: StringProperty(
        name="NR 运行时目录",
        description="ComfyUI-DLSS5-NR 的安装根目录(内含 native/bin 与 runtime)",
        subtype="DIR_PATH", default=DEFAULT_NR_ROOT)
    nr_preset: EnumProperty(name="风格", items=PRESET_ITEMS, default="TEXTURE",
                            description="风格预设 = 模型风格 × 色调 × 结构 × 皮肤 × 合成方式 的组合;"
                                        "选了会覆盖下面的参数,想手调选「自定义」",
                            update=_on_preset_change)
    nr_style: EnumProperty(name="模型风格", items=STYLE_ITEMS, default="2",
                           description="模型内置的 3 种风格(3 以上实测和电影感相同)",
                           update=_on_model_param_change)
    nr_mode: EnumProperty(name="合成方式", items=MODE_ITEMS, default="COLOR",
                          description="模型输出怎么和原图合成;改它不用重跑模型",
                          update=_on_composite_change)
    nr_strength: FloatProperty(name="强度", default=1.0, min=0.0, max=2.0,
                               description="0 = 原图,1 = 完整效果,>1 夸张化;改它不用重跑模型",
                               update=_on_composite_change)
    nr_tone: FloatProperty(name="色调重打光", default=0.0, min=0.0, max=2.0,
                           description="模型的整体重打光力度。0 = 颜色基本不跑(推荐);"
                                       "2 = 整张图发灰发暗的那种'电影感'",
                           update=_on_model_param_change)
    nr_structure: FloatProperty(name="结构细节", default=2.0, min=0.0, max=2.0,
                                description="材质与结构重写力度,皮肤毛孔/唇纹主要靠它,2 最强",
                                update=_on_model_param_change)
    nr_skin: FloatProperty(name="皮肤细节", default=1.5, min=-1.0, max=2.0,
                           description="皮肤区域的额外细节;-1 = 用模型默认",
                           update=_on_model_param_change)
    nr_automask: BoolProperty(name="自动遮罩", default=True,
                              description="让模型自动判断哪些区域应用效果",
                              update=_on_model_param_change)
    nr_live: BoolProperty(name="实时模式", default=False, options={"SKIP_SAVE"})
    nr_live_capture: EnumProperty(
        name="GPU 路抓取方式",
        items=[("AUTO", "自动", "依次尝试:DWM 窗口表面 -> 按窗口捕获 -> 桌面复制"),
               ("DWM", "DWM 窗口表面(无黄框,可录屏)", "直接读 DWM 为 Blender 窗口合成的表面:没有捕获会话,"
                                                    "所以没有 Win10 的黄色边框;录屏/截图都能看到浮窗"),
               ("WGC", "按窗口捕获(可录屏)", "Windows.Graphics.Capture 只抓 Blender 窗口:录屏/截图都能看到浮窗;"
                                             "Win10 会在 Blender 窗口边缘画一圈黄色捕获边框(Win11 没有)"),
               ("DDA", "桌面复制(录屏看不到浮窗)", "抓整个桌面,浮窗必须对录屏隐藏(否则会把自己再喂给自己)")],
        default="AUTO")
    nr_route: EnumProperty(
        name="实时路线",
        items=[("AUTO", "自动", "有 dlss5_live.exe 就走 GPU 路,否则 CPU 路"),
               ("GPU", "GPU", "dlss5_live.exe:窗口表面 -> D3D12 -> NGX -> 浮窗,全分辨率 50-60 帧"),
               ("CPU", "CPU", "Python 工作进程:PrintWindow -> numpy -> NGX -> GDI 浮窗,约 20 帧")],
        default="AUTO")
    nr_live_exe: StringProperty(name="dlss5_live.exe", subtype="FILE_PATH", default="",
                                description="GPU 路的可执行文件;留空则找扩展目录 bin/")
    nr_live_half: BoolProperty(name="实时用半分辨率", default=True,
                               description="实时模式把帧缩到一半再送模型(细节以亮度增益乘回全分辨率原图),"
                                           "帧率翻倍,细节略糊;大视口时用",
                               update=_on_model_param_change)
    nr_iters: IntProperty(name="迭代次数", default=1, min=1, max=8,
                          description="同一帧反复喂给模型累积时间历史;实测只多 ~1% 锐度,一般 1 即可")
    nr_scale: EnumProperty(name="采样倍率", items=SCALE_ITEMS, default="AUTO",
                           description="抓视口时的超采样倍率。模型对 1080p 以上输入效果最好")
    nr_split: FloatProperty(name="分割线", default=0.5, min=0.0, max=1.0, subtype="FACTOR",
                            description="左边原视口,右边 NR。在视口里鼠标靠近黄线按住左键就能拖",
                            update=_on_split_change)
    nr_view: EnumProperty(name="对比方式", items=VIEW_ITEMS, default="SPLIT",
                          description="分割对比 / 只看 NR / 只看原图;快捷键可在偏好设置里给 dlssnr.cycle_view 绑",
                          update=_on_split_change)
    nr_auto: BoolProperty(name="视角稳定后自动刷新", default=True,
                          description="拖动视角时叠加层自动隐藏;停下 0.35 秒后自动重新抓取并处理")
    save_dir: StringProperty(name="保存目录", subtype="DIR_PATH", default="//dlss5_nr",
                             description="「另存 PNG」的输出目录")
    nr_last_info: StringProperty(name="上次", default="", options={"SKIP_SAVE"})

    # ---- 引导通道捕获(未改动) ----
    output_dir: StringProperty(name="输出目录", description="引导文件的写入位置",
                               subtype="DIR_PATH", default="//dlssnr_guides")
    capture_animation: BoolProperty(name="整个帧范围", default=False,
                                    description="捕获每一帧,而不是只捕获当前帧。文件很大,请谨慎")
    guide: EnumProperty(name="预览通道", items=GUIDE_ITEMS, default="Depth", update=_on_guide_change)
    live_enabled: BoolProperty(name="实时预览", default=False, options={"SKIP_SAVE"})
    live_interval: FloatProperty(name="刷新间隔(秒)", default=0.25, min=0.05, max=2.0)
    live_fps: FloatProperty(name="实测帧率", default=0.0, options={"SKIP_SAVE"})
    live_max_pct: IntProperty(name="预览分辨率上限(%)", default=25, min=5, max=100,
                              description="渲染在 UI 主线程上跑,数值越高越容易卡住界面")
    live_adaptive: BoolProperty(name="自动降分辨率", default=True)
    live_pct: IntProperty(name="当前分辨率", default=0, options={"SKIP_SAVE"})
    live_ms: FloatProperty(name="单帧耗时(毫秒)", default=0.0, options={"SKIP_SAVE"})
    half_float: BoolProperty(name="16 位浮点(体积减半)", default=False, update=_on_depth_change)


# ==========================================================================
# 界面
# ==========================================================================
class DLSSNR_PT_panel(Panel):
    bl_label = "DLSS 5 神经渲染"
    bl_idname = "DLSSNR_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "DLSS NR"

    def draw(self, context):
        layout = self.layout
        s = context.scene.dlssnr

        col = layout.column(align=True)
        col.prop(s, "nr_preset", text="风格")
        col.prop(s, "nr_strength", slider=True)

        row = layout.row(align=True)
        row.scale_y = 1.5
        if _Live.enabled:
            row.operator("dlssnr.live_toggle", icon="PAUSE", text="停止", depress=True)
        else:
            row.operator("dlssnr.live_toggle", icon="PLAY", text="实时")
        row.operator("dlssnr.nr_once", icon="RENDER_STILL", text="单帧")
        row.operator("dlssnr.clear_overlay", icon="X", text="")

        if _Worker.state == "fail" or (_Live.enabled and _Live.err):
            layout.label(text=(_Worker.err or _Live.err)[:44], icon="ERROR")
        elif _Live.enabled:
            layout.label(text="%s · %.1f 帧/秒 · NR %.1fms%s" % (
                ("GPU·" + _Live.capture.upper() if _Live.capture else "GPU") if _Live.route == "gpu" else "CPU",
                _Live.fps, _Live.nr_ms, "" if _Live.visible else " · 等待画面"), icon="TIME")
            layout.prop(s, "nr_live_half", text="半分辨率(更流畅)")

        if _Overlay.tex is not None or _Live.enabled:
            layout.row(align=True).prop(s, "nr_view", expand=True)
            if s.nr_view == "SPLIT":
                layout.prop(s, "nr_split", slider=True, text="分割线(可在视口拖)")


class DLSSNR_PT_render(Panel):
    bl_label = "渲染结果(F12)"
    bl_idname = "DLSSNR_PT_render"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "DLSS NR"
    bl_parent_id = "DLSSNR_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = context.scene.dlssnr
        layout.operator("dlssnr.nr_render", icon="RENDER_STILL", text="NR 上一帧渲染")
        row = layout.row(align=True)
        row.prop(s, "save_dir", text="")
        row.operator("dlssnr.save_png", icon="FILE_TICK", text="另存 PNG")
        if s.nr_last_info:
            layout.label(text=s.nr_last_info, icon="TIME")


class DLSSNR_PT_advanced(Panel):
    bl_label = "高级"
    bl_idname = "DLSSNR_PT_advanced"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "DLSS NR"
    bl_parent_id = "DLSSNR_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = context.scene.dlssnr
        col = layout.column(align=True)
        col.prop(s, "nr_mode")
        col.prop(s, "nr_style")
        col.prop(s, "nr_tone", slider=True)
        col.prop(s, "nr_structure", slider=True)
        col.prop(s, "nr_skin", slider=True)
        col.prop(s, "nr_automask")
        col = layout.column(align=True)
        col.prop(s, "nr_route")
        col.prop(s, "nr_live_capture")
        col.prop(s, "nr_live_exe", text="")
        col = layout.column(align=True)
        col.prop(s, "nr_scale", text="单帧倍率")
        col.prop(s, "nr_iters", text="单帧迭代")
        col.prop(s, "nr_auto")
        layout.prop(s, "nr_root", text="")
        row = layout.row(align=True)
        row.operator("dlssnr.worker_restart", icon="FILE_REFRESH", text="重启进程")
        row.operator("dlssnr.worker_stop", icon="CANCEL", text="停止进程")
        if _Worker.state == "ok" and _Worker.alive():
            layout.label(text="进程就绪 · %s" % _Worker.info.get("gpu", "")[:22], icon="CHECKMARK")
        elif _Worker.state == "fail":
            layout.label(text=_Worker.err[:44], icon="ERROR")
        else:
            layout.label(text="进程未启动(用时自动启动)", icon="INFO")


class DLSSNR_PT_guides(Panel):
    bl_label = "EEVEE 引导通道捕获"
    bl_idname = "DLSSNR_PT_guides"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "DLSS NR"
    bl_parent_id = "DLSSNR_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = context.scene.dlssnr
        scene = context.scene
        if scene.render.engine != "BLENDER_EEVEE":
            layout.label(text="需要 EEVEE 引擎", icon="ERROR")
        col = layout.column(align=True)
        col.prop(s, "output_dir", text="")
        col.prop(s, "capture_animation")
        col.prop(s, "half_float")
        if s.capture_animation:
            frames = scene.frame_end - scene.frame_start + 1
            big = (scene.render.resolution_x * scene.render.resolution_y * 4 * 4 * 5 * frames
                   * (scene.render.resolution_percentage / 100.0) ** 2)
            col.label(text="预计约 %.2f GB" % (big / 1073741824.0), icon="INFO")
        layout.operator("dlssnr.capture_guides", icon="FILE_TICK", text="捕获深度/法线到 EXR")
        if s.live_enabled:
            row = layout.row(align=True)
            row.operator("dlssnr.stop_live", icon="PAUSE", text="停止预览")
            row.label(text="%.1f 帧/秒 @ %d%%" % (s.live_fps, s.live_pct))
        else:
            row = layout.row(align=True)
            row.prop(s, "guide", text="")
            row.operator("dlssnr.live_preview", icon="PLAY", text="预览通道")


# ==========================================================================
# 注册
# ==========================================================================
_CLASSES = (
    DLSSNR_Settings,
    DLSSNR_OT_live_toggle,
    DLSSNR_OT_nr_once,
    DLSSNR_OT_drag_split,
    DLSSNR_OT_split_hover,
    DLSSNR_OT_cycle_view,
    DLSSNR_OT_clear_overlay,
    DLSSNR_OT_nr_render,
    DLSSNR_OT_save_png,
    DLSSNR_OT_worker_restart,
    DLSSNR_OT_worker_stop,
    DLSSNR_OT_live_preview,
    DLSSNR_OT_stop_live,
    DLSSNR_OT_capture_guides,
    DLSSNR_PT_panel,
    DLSSNR_PT_render,
    DLSSNR_PT_advanced,
    DLSSNR_PT_guides,
)


_KEYMAPS = []


def _register_keymaps():
    """插件键位:3D 视图里左键按下 -> 若靠近分割线则拖动,否则放行给默认选择。
    鼠标移动 -> 只更新悬停高亮标志,立刻放行。"""
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
    kmi = km.keymap_items.new("dlssnr.drag_split", "LEFTMOUSE", "PRESS")
    _KEYMAPS.append((km, kmi))
    kmi = km.keymap_items.new("dlssnr.split_hover", "MOUSEMOVE", "ANY")
    _KEYMAPS.append((km, kmi))


def _unregister_keymaps():
    for km, kmi in _KEYMAPS:
        try:
            km.keymap_items.remove(kmi)
        except Exception:                                   # noqa: BLE001
            pass
    _KEYMAPS.clear()


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.dlssnr = PointerProperty(type=DLSSNR_Settings)
    _Overlay.handle = bpy.types.SpaceView3D.draw_handler_add(_overlay_draw, (), "WINDOW", "POST_PIXEL")
    bpy.app.timers.register(_auto_tick, first_interval=0.3, persistent=True)
    try:
        _register_keymaps()
    except Exception:                                       # noqa: BLE001
        pass


def unregister():
    _live_stop()
    _GpuLive.stop()
    _unregister_keymaps()
    if _Overlay.handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_Overlay.handle, "WINDOW")
        except Exception:                                   # noqa: BLE001
            pass
        _Overlay.handle = None
    _Overlay.tex = None
    try:
        if bpy.app.timers.is_registered(_auto_tick):
            bpy.app.timers.unregister(_auto_tick)
    except Exception:                                       # noqa: BLE001
        pass
    _Worker.stop()
    _Last.orig = _Last.nr = _Last.out = _Last.orig_small = _Last.nr_small = None
    if hasattr(bpy.types.Scene, "dlssnr"):
        del bpy.types.Scene.dlssnr
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
