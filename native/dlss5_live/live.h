// live.h - DLSS 5 Neural Rendering live overlay for a window region (the Blender viewport).
//
// GPU route: DXGI Desktop Duplication captures the desktop into a D3D11 texture -> the viewport
// rectangle is copied into a shared texture -> D3D12 decodes it (sRGB -> linear, optional 2x
// downsample), sRGB-encodes it for the model, runs NVOF motion vectors, evaluates NGX feature 18,
// composites the model output over the original and writes premultiplied BGRA straight into a
// DirectComposition swap chain of a click-through topmost window that sits over the viewport.
// No pixel crosses to the CPU except a 32x32 luma thumbnail used to skip unchanged frames.
//
// Control: JSON lines on stdin, status lines on stdout (see RunLive).
#pragma once

#include <string>

struct LiveArgs {
    std::string dllDir;      // where nvngx_dlssnr.dll lives (the forwarder must sit next to the exe)
    int adapter = -1;
    bool verbose = false;
    bool headless = false;   // never show the window (tests)
};

int RunLive(const LiveArgs& args);
