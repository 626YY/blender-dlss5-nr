"""DLSS 5 神经渲染工作进程(在 Blender 之外的独立 Python 里跑 NGX)。

为什么独立进程:NGX / D3D12 / 社区改版的 nvngx_dlssnr.dll 一旦崩溃或死锁,
只会死掉这个小进程,Blender 和你没保存的工程毫发无损。停用扩展时直接
把它结束,显存跟着释放(进程内加载则永远卸不掉,shutdown 在该驱动上死锁)。

协议(stdin / stdout 二进制):
  每条消息 = b"NRW1" + 4 字节小端 JSON 长度 + JSON + 可选二进制载荷
  JSON 里 "bytes" 字段 = 载荷长度。
  启动后先回一条 {"ok":1,"version":..,"gpu":..} 或 {"ok":0,"err":..}。

  请求字段:
    w, h                 尺寸
    in    "rgb32f"(默认,float32 RGB 顶行在前)| "rgba8"(uint8 RGBA 底行在前,即 GL 帧缓冲原样)
    out   "rgb32f"(默认)| "rgba32f_gl"(float32 RGBA 底行在前,可直接喂 GPUTexture)
    style, tone, structure, skin, automask, preset
    iters(>1 时同帧反复迭代)、temporal(NVOF 光流)、reset(显式;省略则首次迭代重置)
    mode, strength       给了就在这里做合成(COLOR / FULL / DETAIL),主线程零负担
  回应 {"ok":1,"ms":..,"nr_ms":..,"bytes":..} + 结果;或 {"ok":0,"err":..}

用法: python nr_worker.py <ComfyUI-DLSS5-NR 根目录> [gpu_index]
"""
import ctypes
import json
import os
import struct
import sys
import time

MAGIC = b"NRW1"


def _send(out, obj, payload=b""):
    if payload:
        obj["bytes"] = len(payload)
    js = json.dumps(obj).encode("utf-8")
    out.write(MAGIC + struct.pack("<I", len(js)) + js)
    if payload:
        out.write(payload)
    out.flush()


def _read_exact(inp, n):
    chunks, got = [], 0
    while got < n:
        c = inp.read(n - got)
        if not c:
            return None
        chunks.append(c)
        got += len(c)
    return b"".join(chunks)


def _recv(inp):
    head = _read_exact(inp, 8)
    if head is None or head[:4] != MAGIC:
        return None, None
    (n,) = struct.unpack("<I", head[4:8])
    js = _read_exact(inp, n)
    if js is None:
        return None, None
    obj = json.loads(js.decode("utf-8"))
    nbytes = int(obj.get("bytes", 0))
    payload = _read_exact(inp, nbytes) if nbytes else b""
    if payload is None:
        return None, None
    return obj, payload


def main():
    out = sys.stdout.buffer
    inp = sys.stdin.buffer
    sys.stdout = sys.stderr          # 任何 print 都走 stderr,不污染数据流
    root = sys.argv[1]
    gpu_index = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    bridge = os.path.join(root, "native", "bin", "dlss5nr_bridge.dll")
    runtime = os.path.join(root, "runtime")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import numpy as np
        import nr_math
    except Exception as exc:                                # noqa: BLE001
        _send(out, {"ok": 0, "err": "worker 缺 numpy / nr_math: %s" % exc})
        return
    if not os.path.exists(bridge):
        _send(out, {"ok": 0, "err": "桥接 DLL 不存在: %s" % bridge})
        return
    if not os.path.exists(os.path.join(runtime, "nvngx_dlssnr.dll")):
        _send(out, {"ok": 0, "err": "模型 nvngx_dlssnr.dll 不存在: %s" % runtime})
        return
    handles = []
    for d in (os.path.dirname(bridge), runtime, os.path.join(runtime, "caller")):
        if os.path.isdir(d):
            try:
                handles.append(os.add_dll_directory(d))
            except Exception:                               # noqa: BLE001
                pass
    try:
        lib = ctypes.WinDLL(bridge)
        lib.dlss5nr_init.argtypes = [ctypes.c_int, ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_int]
        lib.dlss5nr_init.restype = ctypes.c_int
        lib.dlss5nr_process.argtypes = [
            ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_int]
        lib.dlss5nr_process.restype = ctypes.c_int
        lib.dlss5nr_version.restype = ctypes.c_char_p
        lib.dlss5nr_gpu_name.restype = ctypes.c_char_p
        err = ctypes.create_string_buffer(4096)
        rc = lib.dlss5nr_init(gpu_index, runtime, err, len(err))
        if rc == 0:
            _send(out, {"ok": 0, "err": "NGX init 失败: %s" % err.value.decode("utf-8", "replace")})
            return
        ver = (lib.dlss5nr_version() or b"?").decode("utf-8", "replace")
        gpu = (lib.dlss5nr_gpu_name() or b"?").decode("utf-8", "replace")
    except Exception as exc:                                # noqa: BLE001
        _send(out, {"ok": 0, "err": "加载桥接失败: %s" % exc})
        return
    _send(out, {"ok": 1, "version": ver, "gpu": gpu, "pid": os.getpid()})
    import threading
    ngx_lock = threading.Lock()          # NR 监视窗线程和这里的同步请求共用一个 NGX feature
    monitor = None

    while True:
        req, payload = _recv(inp)
        if req is None or req.get("cmd") == "quit":
            break
        if req.get("cmd") == "monitor":
            try:
                action = req.get("action")
                if monitor is None:
                    import nr_monitor
                    monitor = nr_monitor.Monitor(lib, ngx_lock)
                if action == "start":
                    ok, err = monitor.start(req.get("cfg", {}))
                    _send(out, {"ok": 1 if ok else 0, "err": err})
                elif action == "update":
                    monitor.update(req.get("cfg", {}))
                    _send(out, dict(ok=1, **monitor.status()))
                elif action == "stop":
                    monitor.stop()
                    _send(out, {"ok": 1})
                else:
                    _send(out, dict(ok=1, **monitor.status()))
            except Exception as exc:                        # noqa: BLE001
                _send(out, {"ok": 0, "err": "monitor: %s" % exc})
            continue
        try:
            t_all = time.perf_counter()
            w, h = int(req["w"]), int(req["h"])
            n = w * h * 3
            fmt_in = req.get("in", "rgb32f")
            if fmt_in == "rgba8":
                if len(payload) != w * h * 4:
                    raise ValueError("载荷长度不对: %d != %d" % (len(payload), w * h * 4))
                a = np.frombuffer(payload, dtype=np.uint8).reshape(h, w, 4)[::-1, :, :3]
                frame = np.ascontiguousarray(a, dtype=np.float32) * (1.0 / 255.0)
            else:
                if len(payload) != n * 4:
                    raise ValueError("载荷长度不对: %d != %d" % (len(payload), n * 4))
                frame = np.frombuffer(payload, dtype=np.float32).reshape(h, w, 3).copy()
            result = np.empty((h, w, 3), dtype=np.float32)
            iters = max(1, int(req.get("iters", 1)))
            temporal = int(req.get("temporal", 1)) if (iters > 1 or "reset" in req) else 0
            t0 = time.perf_counter()
            for i in range(iters):
                if "reset" in req and i == 0:
                    reset = 1 if req["reset"] else 0
                else:
                    reset = 1 if i == 0 else 0
                e = ctypes.create_string_buffer(4096)
                with ngx_lock:
                    rc = lib.dlss5nr_process(
                        frame.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                        result.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                        w, h, int(req.get("style", 2)), int(req.get("preset", 0)),
                        1.0,                     # intensity: 实测 >=1 无效,<1 只是整体变暗,固定 1
                        float(req.get("tone", 0.0)), float(req.get("structure", 2.0)),
                        float(req.get("skin", 1.5)),
                        1 if req.get("automask", True) else 0,
                        reset, temporal,
                        e, len(e))
                if rc == 0:
                    raise RuntimeError(e.value.decode("utf-8", "replace"))
            nr_ms = (time.perf_counter() - t0) * 1000.0
            outimg = result
            if "mode" in req:
                outimg = nr_math.composite(frame, result, req["mode"], float(req.get("strength", 1.0)))
            if req.get("out", "rgb32f") == "rgba32f_gl":
                rgba = np.empty((h, w, 4), dtype=np.float32)
                rgba[..., :3] = outimg[::-1]
                rgba[..., 3] = 1.0
                data = rgba.tobytes()
            else:
                data = np.ascontiguousarray(outimg, dtype=np.float32).tobytes()
            _send(out, {"ok": 1, "ms": round((time.perf_counter() - t_all) * 1000.0, 1),
                        "nr_ms": round(nr_ms, 1), "iters": iters}, data)
        except Exception as exc:                            # noqa: BLE001
            _send(out, {"ok": 0, "err": str(exc)})
    if monitor is not None:
        try:
            monitor.stop()
        except Exception:                                   # noqa: BLE001
            pass
    # 故意不调 dlss5nr_shutdown(该驱动上会死锁);进程退出由系统回收
    os._exit(0)


if __name__ == "__main__":
    main()
