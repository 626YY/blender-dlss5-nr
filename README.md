# blender-dlss5-nr

**DLSS 5 Neural Rendering inside Blender** — a live, click-through overlay that runs NVIDIA's
DLSS 5 Neural Rendering (NGX feature 18) over your viewport at 50–60 fps, plus one-shot high-res
viewport / F12 processing with an A/B split slider.

[中文说明在下面](#中文)

> **Experimental. Unofficial.** DLSS 5 Neural Rendering is an undocumented, pre-release NVIDIA
> feature; the model (`nvngx_dlssnr.dll`) is not part of this repository and is only known to run on
> RTX 40 through a community-patched build. Nothing here is affiliated with or endorsed by NVIDIA or
> the Blender Foundation. Driver/runtime faults can crash the helper process (never Blender itself —
> everything NGX runs out of process).


## What it does

| Mode | How | Speed (RTX 4080 SUPER, 1353×1056 viewport) |
|---|---|---|
| **Live — GPU route** | `dlss5_live.exe`: Windows.Graphics.Capture of the Blender window (or DXGI Desktop Duplication) → D3D12 → NVOF motion vectors → NGX feature 18 → composite → DirectComposition overlay. No pixel leaves the GPU. | **52–68 fps** full res, 75–95 fps half res; the model itself takes 2–4 ms |
| **Live — CPU route** | Python worker: `PrintWindow` → numpy → ComfyUI-DLSS5-NR's bridge DLL → GDI layered window | ~10 fps full res, ~20 fps half res |
| **Single frame** | GPU offscreen capture of the viewport at 2× → NR → overlay with split slider | ~1 s |
| **F12 result** | Processes the last render, writes `DLSS5_NR` image, saves PNG | ~1 s |

The overlay is a click-through window **owned by the Blender window**, sitting exactly over the 3D
viewport: EEVEE keeps running at full speed underneath, you orbit/edit as usual, the NR view follows.
Because it is owned, Windows keeps it in Blender's z-order: whatever covers Blender covers it, it
minimises with Blender, and it never shows over other applications. Screen recorders and screenshot
tools see it like any other window. A draggable yellow split line compares original (left) and NR
(right); Blender's own panels are cut out of the overlay.

Three composite modes: **keep original colour** (model luminance detail on your colours — default),
**full NR** (the model's output incl. its relighting), **detail only** (high-frequency detail added
back to your lighting; CPU route / single frame only).

## Requirements

- Windows 10 2004+ / 11, NVIDIA RTX 40/50 (RTX 20/30 untested), driver **616.56+**.
- Blender **5.0+** (developed on 5.1.2, OpenGL backend).
- [ComfyUI-DLSS5-NR](https://github.com/lisitskyaa/ComfyUI-DLSS5-NR) v0.3.1 installed somewhere:
  the add-on reads `native/bin/dlss5nr_bridge.dll` (CPU route) and `runtime/nvngx_dlssnr.dll`
  (both routes) from that folder. **You supply `nvngx_dlssnr.dll` yourself**; this project ships no
  NVIDIA binaries.
- GPU route: `dlss5_live.exe` + `nvngx.dll_dlssnr.dll` in `addon/dlss5_nr/bin/` (build below, or
  take them from a Release).

## Install

1. Zip the `addon/dlss5_nr` folder (or use the Release zip) and install it in Blender:
   *Edit → Preferences → Get Extensions → ⌄ → Install from Disk*.
2. In the 3D viewport sidebar (N) → **DLSS NR** → *高级/Advanced* → set **NR 运行时目录** to your
   `ComfyUI-DLSS5-NR` folder.
3. Put the GPU binaries in the extension's `bin/` folder (see `addon/dlss5_nr/bin/README.md`).
   Without them the add-on falls back to the CPU route automatically.

## Use

- **实时 / Live**: toggles the overlay on the viewport you clicked in. Drag the yellow line to
  compare; the three buttons switch *split / NR only / original*. Half-resolution mode doubles the
  frame rate; on the GPU route full resolution is already real-time.
- **单帧 / Single**: one high-quality frame (2× supersampled capture), same split compare.
- **Recording**: with the default per-window capture (*高级 → 抓取方式 → 按窗口*) OBS / screenshot
  tools record the overlay. Windows 10 draws a yellow border around a window that is being captured
  this way (Windows 11 lets the app turn it off). If the border bothers you, switch to *桌面复制*
  (desktop duplication): no border, but the overlay must then exclude itself from capture, so
  recorders will not see it.
- **渲染结果 / F12**: after a render, *NR 上一帧渲染*, then *另存 PNG*.
- **风格 / Style presets**: the model only has three real styles (0/1/2 — indices 3+ are identical
  to 2); the presets combine style × tone × structure × skin × composite mode. See
  [docs/FINDINGS.md](docs/FINDINGS.md) for what the knobs really do (two of the model's advertised
  parameters, `intensity` and `preset`, do nothing).

## Build the GPU route

```bat
git clone --recurse-submodules https://github.com/626YY/blender-dlss5-nr
cd blender-dlss5-nr
:: get nvsdk_ngx_d.lib from https://github.com/NVIDIA/DLSS (lib/Windows_x86_64/x64/)
copy nvsdk_ngx_d.lib native\video2dlssnr\third_party\nvngx\lib\
native\dlss5_live\build.bat
copy native\dlss5_live\out\dlss5_live.exe addon\dlss5_nr\bin\
copy native\dlss5_live\out\nvngx.dll_dlssnr.dll addon\dlss5_nr\bin\
```

Needs an MSVC x64 toolchain (Visual Studio 2022+ / Build Tools). No admin rights? A portable MSVC works:
[docs/BUILD.md](docs/BUILD.md).

## How it works / what we learned

[docs/FINDINGS.md](docs/FINDINGS.md) — the parameter sweeps (which knobs are real), why Blender's
framebuffer cannot be read from Python in 5.1 (EEVEE resolves after the draw callbacks), the
`gpu.types.Buffer` stride bug, the measured cost of the model, and the whole capture → NR → present
pipeline of the GPU route.

## Credits / third-party

- [video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr) (MIT) — the D3D12/NGX/NVOF base the
  GPU route is built on (git submodule); its forwarder shim is what lets a non-driver caller drive the
  snippet.
- [ComfyUI-DLSS5-NR](https://github.com/lisitskyaa/ComfyUI-DLSS5-NR) (MIT) — the CPU-staging bridge
  the CPU route calls, and the parameter contract everyone reverse-engineered.
- NVIDIA DLSS SDK headers/import library — NVIDIA's own licence, not redistributed here.
- The overlay concept follows [neural-lens](https://github.com/Leaps-Bounds/neural-lens).

See [THIRD_PARTY.md](THIRD_PARTY.md). This project's own code is MIT.

---

## 中文

**在 Blender 里实时跑 DLSS 5 神经渲染**：一个盖在视口上的点击穿透浮窗，EEVEE 照常全速跑，
NR 画面以 50–60 帧跟着动；另有单帧高清、F12 结果处理，带可拖的左右对比线。

> 实验性、非官方。DLSS 5 神经渲染是 NVIDIA 未公开的预发布功能，模型文件 `nvngx_dlssnr.dll`
> 不在本仓库里，RTX 40 只能靠社区改版跑。所有 NGX 代码都在独立进程里执行，崩了不会带走 Blender。

**两条路：**
- **GPU 路** `dlss5_live.exe`（C++）：按窗口抓取（Windows.Graphics.Capture，可切桌面复制）→ D3D12 → NVOF 运动矢量 → NGX → 合成 → DirectComposition
  浮窗，全程不下 GPU。4080 SUPER 上 1353×1056 全分辨率 52–68 帧，半分辨率 75–95 帧，模型本身 2–4 ms。
- **CPU 路**（Python 工作进程）：PrintWindow → numpy → ComfyUI-DLSS5-NR 的桥 DLL → GDI 分层窗，约 10–20 帧。
  没有 exe 时自动退回这条路。

**安装：** 打包 `addon/dlss5_nr` 装进 Blender 5.x；侧边栏 DLSS NR → 高级 → 填 ComfyUI-DLSS5-NR 目录
（要自备 `runtime/nvngx_dlssnr.dll`）；把 `dlss5_live.exe` 和 `nvngx.dll_dlssnr.dll` 放进扩展的 `bin/`。

**用法：** 「实时」开关浮窗，拖黄线对比，三个按钮切 分割/只看 NR/只看原图；「单帧」出一张 2× 超采样的高清
对比；F12 之后用「NR 上一帧渲染」「另存 PNG」。风格预设 = 模型风格 × 色调 × 结构 × 皮肤 × 合成方式的组合
（模型真正的风格只有 3 种；`intensity` 和 `preset` 两个参数实测无效）。

**浮窗与录屏：** 浮窗是 Blender 窗口的从属窗口，只会出现在 Blender 正上方：别的窗口盖住 Blender 它就一起被盖住，
Blender 最小化它也跟着藏，不会漏到别的程序上面；录屏 / 截图软件能正常录到它。Win10 上「按窗口」抓取会有系统画的
黄色边框（Win11 可以关掉），嫌碍事就到「高级 → 抓取方式」切「桌面复制」，但那样浮窗对录屏就是隐形的。

**编译 GPU 路：** 见上面的命令和 [docs/BUILD.md](docs/BUILD.md)（无管理员权限可以用便携 MSVC）。

**原理与踩坑：** [docs/FINDINGS.md](docs/FINDINGS.md)。
