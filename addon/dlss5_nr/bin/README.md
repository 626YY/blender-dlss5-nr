# bin/ — GPU route binaries (not committed)

Put these two files here to enable the GPU route (`dlss5_live.exe`):

- `dlss5_live.exe` — build it with `native/dlss5_live/build.bat`, or download it from the Releases page.
- `nvngx.dll_dlssnr.dll` — the caller-gate forwarder shim, built by the same script
  (source: `native/video2dlssnr/forwarder/nvngx_fwd.cpp`, MIT).

The NVIDIA model itself (`nvngx_dlssnr.dll`) is **not** shipped by this project. The add-on reads it
from `<NR root>/runtime/`, the same folder ComfyUI-DLSS5-NR uses.
