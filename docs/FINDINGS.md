# Findings

Everything below was measured on one machine: RTX 4080 SUPER, driver 616.64, Windows 10 22H2,
Blender 5.1.2 (OpenGL backend), `nvngx_dlssnr.dll` 310.8.SF (community RTX 40 build),
ComfyUI-DLSS5-NR 0.3.1 bridge, video2dlssnr 1.4.1. Inputs were 1080p–1353×1056 viewport captures of
a stylised character head. Your numbers will differ; the *shapes* of the results should not.

## 1. What the model's parameters really do

Sweeps with the bridge on a 1920×1080 material-preview capture (mean |diff| to the input, 0–1):

| Knob | Effect |
|---|---|
| `style` 0 / 1 / 2 | Three distinct outputs: 0 desaturates, 1 sharpest and slightly brighter, 2 the mildest colour drift. **Values 3–9 are bit-identical to 2.** |
| `preset` 0–3 | **No effect at all** (12 style×preset combinations, byte-identical per style). |
| `intensity` | **No effect for ≥ 1.0**; below 1.0 it just scales the whole output darker (0.5 → half brightness). It is a gain, not a detail knob. |
| `tone` (LocalToneStrength) | The relighting. 0 keeps colours essentially intact; 2 is the grey/dark "cinematic" look people complain about. |
| `structure` (LocalStructureStrength) | The actual detail knob: pores, lip texture, lashes. 2 = strongest. |
| `skin` (SkinStructureStrength) | Small extra effect on skin; 1.5–2 fine. |
| tone = structure = skin = 0 | Near-identity (diff 0.0005). |
| temporal iterations on the same frame | +1.3 % sharpness after 16 iterations. Not worth it for stills. |

Consequences for the add-on: `intensity` and `preset` are not exposed; defaults are style 2,
tone 0, structure 2, skin 1.5; "styles" in the UI are presets over the knobs that matter.

## 2. Compositing

The model's raw output relights and shifts colour. Three ways to put it back on the original:

- **keep colour** (default): `out = orig × luma(nr) / luma(orig)` — the model's luminance detail on
  the original chroma. This is also what `video2dlssnr --nr-color 0` does.
- **full**: the raw output (blend by strength).
- **detail only**: `Y = Y_orig + (HF(Y_nr) − HF(Y_orig))`, HF = signal minus a ~6 px low-pass.
  Keeps the original lighting exactly. The low-pass is done at ¼ resolution (4× box downsample →
  3-pass box blur → nearest upsample); at full resolution it was 10× slower for no visible gain.

On the GPU route the composite is video2dlssnr's compute shader (`detail`, `colour`), so *detail
only* is mapped to *keep colour* there.

## 3. Cost of the model

video2dlssnr's own timing table (which includes CPU staging) says "NR evaluate 75 ms" at 1353×1056.
With the frame already on the GPU and the feature kept alive, GPU timestamps around
`EvaluateFeature` read **3.5–4.3 ms at 1352×1056 and 2.3 ms at 676×528**. The CPU-staging bridge
measured 60 ms per frame at 1353×1056 including its uploads/downloads. So the model is cheap; the
copies are not. That is the whole argument for the GPU route.

## 4. Getting the viewport out of Blender 5.1

- `gpu.types.Buffer` exposes a **column-major stride** through the buffer protocol
  (`memoryview.strides == [1, h, h*w]`), so `numpy.asarray(buf)` produces diagonal stripes. The
  memory is row-major; reinterpret with `as_strided` (`nr_math.fix_gpu_buffer`).
- `GPUOffScreen.draw_view3d` works in SOLID and MATERIAL shading and honours scene lights/world; in
  RENDERED shading it falls back to the studio look. A full-region draw costs 150–190 ms regardless
  of resolution and blocks the main thread — fine for the single-frame mode, useless for live.
- Reading the viewport's framebuffer from a draw handler (`active_framebuffer_get().read_color()`)
  returns only the **overlay layer** in every stage (`PRE_VIEW` / `POST_VIEW` / `POST_PIXEL`):
  background grey plus alpha-0 silhouettes where geometry is. EEVEE Next and Workbench resolve their
  colour after the callbacks. Independent of shading mode, engine, or the overlays toggle.
- `bpy.ops.render.opengl(view_context=True)` gives a correct capture but goes through a PNG on disk
  and overwrites *Render Result*.
- `PrintWindow(hwnd, PW_CLIENTONLY | PW_RENDERFULLCONTENT)` returns Blender's DWM-composed window
  including the GL viewports (16–30 ms for 2560×1377). That is the CPU route's capture.
- `DwmGetDxSharedSurface` (undocumented, exported by user32 since Windows 8) hands out the DWM
  redirection surface of a window as a shareable D3D11 texture: `B8G8R8A8_UNORM`, the size of
  `GetWindowRect` (including the invisible resize border; client origin = `ClientToScreen(0,0)` minus
  the window rect's top-left). No capture session, so no Windows 10 capture border, no CPU copy, and
  the surface never contains our overlay. This is the GPU route's default capture. The update id it
  returns ticks with every composition, so the 32×32 luma thumbnail still decides whether the picture
  changed. Falls back to the two below when the call fails or the window lives on another GPU.
- `Windows.Graphics.Capture` of the Blender window (C++/WinRT: `IGraphicsCaptureItemInterop::
  CreateForWindow`, a free-threaded `Direct3D11CaptureFramePool`) gives the window's DWM surface as a
  D3D11 texture with zero CPU involvement, and — being per-window — never contains our own overlay,
  so the overlay can stay visible to screen recorders. First fallback
  (verified: with a static viewport the frame counter stops at 1). Windows 10 19045 rejects
  `IsBorderRequired = false`, so the yellow capture border stays there; Windows 11 honours it.
- DXGI Desktop Duplication is the fallback (`capture: "dda"`): same texture path, but it sees the
  whole screen, so the overlay window must set `WDA_EXCLUDEFROMCAPTURE` to avoid feeding itself
  back — which also hides it from recorders. No border.

## 5. The GPU route, per frame

1. Read the window's DWM surface (`DwmGetDxSharedSurface`), or `TryGetNextFrame` with window capture,
   or `AcquireNextFrame` with desktop duplication (skipping frames whose dirty/move rects miss the
   viewport); copy the viewport rectangle into a shared `B8G8R8A8` texture; D3D11 `Signal` a shared
   fence.
2. D3D12 `Wait` on that fence → decode pass (sRGB → linear RGBA16F, optional 2×2 average for the
   half-resolution mode) + a 32×32 luma thumbnail (read back, 4 KB, to skip unchanged pictures).
3. `RecordEncodeSrgb` (what the model expects) → NVOF motion vectors (previous frame → current) →
   `fwd_evaluate` (feature 18, temporal, no reset) → `RecordComposite` (linear original + model
   output → 8-bit sRGB at NR resolution).
4. Present pass: split line, panel cut-outs, premultiplied alpha; half-resolution mode applies the
   low-resolution luminance gain to the full-resolution capture so the base stays sharp; copy into a
   DirectComposition swap chain; `Present(1)`.
5. Control: one JSON object per line on stdin (`start` / `update` / `status` / `stop`), one JSON
   line back. The add-on pushes the viewport rectangle and the panel rectangles ("holes") every
   100 ms when they change.

Measured: 52–68 fps at 1352×1056 (frame 13 ms, four CPU-GPU syncs of which most could be removed),
75–95 fps at 676×528.

## 6. Things that did not work

- `PRE_VIEW` framebuffer reads return a stale buffer (even the previous shading mode's image).
- `UpdateLayeredWindow` from numpy tops out around 20 fps at 1.4 MP; fine as a fallback.
- Letting the overlay draw only when the view is stable ("hide while orbiting") reads as flicker
  and was rejected by the user; the overlay now follows continuously.
- A `WS_EX_TOPMOST` overlay plus a "hide when another window is in front" heuristic: it leaked over
  other applications whenever the heuristic misjudged, and vanished the moment a screenshot tool
  opened its own window. Replaced by an *owned* popup of the Blender window (`hWndParent` = Blender):
  Windows itself keeps it directly above Blender, under anything that covers Blender, and hides it
  when Blender is minimised. `scripts/test_zorder.py` checks all three with a BitBlt of the screen.
