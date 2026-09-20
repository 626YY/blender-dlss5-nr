# Building `dlss5_live.exe`

## With Visual Studio

Install Visual Studio 2022+ (or Build Tools) with the *Desktop development with C++* workload.
`native\dlss5_live\build.bat` finds `vcvars64.bat` on its own.

## Without admin rights (portable MSVC)

The tool was developed on a machine with no administrator account and no Visual Studio. This works:

```bat
:: mmozeiko's downloader pulls the official MSVC + Windows SDK packages into a folder
python portable-msvc.py --accept-license --msvc-version 14.44 --sdk-version 26100 --target x64 --host x64
:: -> .\msvc\setup_x64.bat
set MSVC_PORTABLE=%CD%\msvc
native\dlss5_live\build.bat
```

`portable-msvc.py`: https://gist.github.com/mmozeiko/7f3162ec2988e81e56d5c4e22cde9977 (about 1.2 GB on disk).
`build.bat` also looks for `tools\msvc\setup_x64.bat` two levels up from itself.

## The NGX import library

`native\video2dlssnr\third_party\nvngx\lib\nvsdk_ngx_d.lib` (and `_dbg` for debug builds) must be
copied from NVIDIA's DLSS SDK repository: https://github.com/NVIDIA/DLSS, folder
`lib/Windows_x86_64/x64/`. It is NVIDIA-licensed and deliberately not in this repository.

## Runtime files

- `nvngx_dlssnr.dll` — the DLSS 5 Neural Rendering model. Not shipped. Point the add-on's
  *NR 运行时目录* at your ComfyUI-DLSS5-NR folder; `runtime/nvngx_dlssnr.dll` is used.
- `nvngx.dll_dlssnr.dll` — built by `build.bat` from video2dlssnr's forwarder. Must sit next to
  `dlss5_live.exe` (the snippet checks the caller module's name).

## Gotchas hit while building

- A batch `/I "path\"` argument whose path ends in a backslash swallows the closing quote (MSVC
  command-line escaping). Write `"path\."` instead.
- `CreateSwapChainForComposition` rejects `DXGI_USAGE_UNORDERED_ACCESS`; the present pass writes an
  intermediate texture and `CopyResource`s it into the back buffer.
- Blender 5.1's OpenGL backend: `GPUOffScreen.draw_view3d` works, but only in MATERIAL/SOLID shading
  (in RENDERED it ignores scene lights); the Python `read_color()` of the viewport framebuffer only
  ever returns the overlay layer. Hence the desktop-duplication design of the GPU route.
