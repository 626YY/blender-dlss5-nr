# Third-party components

This repository's own code (`addon/`, `native/dlss5_live/`, `scripts/`, `docs/`) is MIT-licensed.
It builds on and talks to the following, which keep their own licences:

| Component | Licence | How it is used |
|---|---|---|
| [video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr) by Daniil Sokolyuk | MIT | Git submodule at `native/video2dlssnr`. `dlss5_live.exe` compiles its `src/` (D3D12 context, NGX session, feature-18 parameter contract, NVOF/LK optical flow, sRGB encode + composite shaders) and its forwarder shim `nvngx.dll_dlssnr.dll`. |
| [ComfyUI-DLSS5-NR](https://github.com/lisitskyaa/ComfyUI-DLSS5-NR) by lisitskyaa | MIT | The CPU route loads its `native/bin/dlss5nr_bridge.dll` at runtime; not vendored. |
| NVIDIA DLSS SDK (`nvsdk_ngx*.h`, `nvsdk_ngx_d.lib`) | NVIDIA RTX SDKs licence | Headers are vendored by video2dlssnr; the import library is **not** redistributed — fetch it from https://github.com/NVIDIA/DLSS. |
| NVIDIA Optical Flow SDK headers | NVIDIA licence | Vendored by video2dlssnr. |
| `nvngx_dlssnr.dll`, `nvngx_dlss.dll`, `_nvngx.dll` | NVIDIA proprietary | **Not included.** The user provides them; the add-on reads them from the ComfyUI-DLSS5-NR `runtime/` folder. |
| stb_image / stb_image_write | Public domain / MIT | Vendored by video2dlssnr; used for the debug frame dump. |
| [neural-lens](https://github.com/Leaps-Bounds/neural-lens) | MIT | Not used as code; the "click-through overlay over any window" idea comes from it. |
