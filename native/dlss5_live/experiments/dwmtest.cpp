// Probe: can we read Blender's DWM redirection surface directly (DwmGetDxSharedSurface, undocumented,
// exported by user32.dll)?  No capture session -> no yellow border on Windows 10, no CPU copy, and the
// surface never contains our own overlay window.  Prints geometry, watches the update id, dumps a PNG.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11_1.h>
#include <dxgi1_2.h>
#include <dwmapi.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstdint>
#include <vector>
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"
using Microsoft::WRL::ComPtr;

typedef BOOL(WINAPI* PFN_DwmGetDxSharedSurface)(HWND, HANDLE*, LUID*, ULONG*, ULONG*, ULONGLONG*);

struct Best { HWND h = nullptr; long area = 0; };
static BOOL CALLBACK Enum(HWND h, LPARAM lp) {
    auto* b = reinterpret_cast<Best*>(lp);
    if (!IsWindowVisible(h) || IsIconic(h)) return TRUE;
    wchar_t cls[64] = {}, title[256] = {};
    GetClassNameW(h, cls, 64);
    GetWindowTextW(h, title, 256);
    if (wcsstr(cls, L"GHOST") && wcsstr(title, L"Blender")) {
        RECT r{};
        GetClientRect(h, &r);
        long a = static_cast<long>(r.right) * r.bottom;
        if (a > b->area) { b->area = a; b->h = h; }
    }
    return TRUE;
}

int wmain(int argc, wchar_t** argv) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    Best b;
    EnumWindows(Enum, reinterpret_cast<LPARAM>(&b));
    HWND hwnd = argc > 1 ? reinterpret_cast<HWND>(static_cast<uintptr_t>(wcstoull(argv[1], nullptr, 0))) : b.h;
    if (!hwnd) { printf("no Blender window\n"); return 1; }
    auto fn = reinterpret_cast<PFN_DwmGetDxSharedSurface>(
        GetProcAddress(GetModuleHandleW(L"user32.dll"), "DwmGetDxSharedSurface"));
    if (!fn) { printf("DwmGetDxSharedSurface not exported by user32\n"); return 1; }
    HANDLE h = nullptr;
    LUID luid{};
    ULONG fmt = 0, flags = 0;
    ULONGLONG upd = 0;
    if (!fn(hwnd, &h, &luid, &fmt, &flags, &upd)) { printf("DwmGetDxSharedSurface failed, gle=%lu\n", GetLastError()); return 1; }
    printf("hwnd %p handle %p adapter luid %08lx-%08lx fmt %lu presentFlags %lu updateId %llu\n", hwnd, h,
           luid.HighPart, luid.LowPart, fmt, flags, upd);

    ComPtr<IDXGIFactory1> fac;
    CreateDXGIFactory1(IID_PPV_ARGS(&fac));
    ComPtr<IDXGIAdapter1> ad, pick;
    for (UINT i = 0; fac->EnumAdapters1(i, &ad) != DXGI_ERROR_NOT_FOUND; ++i) {
        DXGI_ADAPTER_DESC1 d{};
        ad->GetDesc1(&d);
        wprintf(L"  adapter %u: %s luid %08lx-%08lx\n", i, d.Description, d.AdapterLuid.HighPart, d.AdapterLuid.LowPart);
        if (d.AdapterLuid.HighPart == luid.HighPart && d.AdapterLuid.LowPart == luid.LowPart) pick = ad;
    }
    ComPtr<ID3D11Device> dev;
    ComPtr<ID3D11DeviceContext> ctx;
    D3D_FEATURE_LEVEL lv[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
    HRESULT hr = D3D11CreateDevice(pick.Get(), pick ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE, nullptr,
                                   D3D11_CREATE_DEVICE_BGRA_SUPPORT, lv, 2, D3D11_SDK_VERSION, &dev, nullptr, &ctx);
    if (FAILED(hr)) { printf("D3D11CreateDevice 0x%08lx\n", hr); return 1; }
    ComPtr<ID3D11Texture2D> tex;
    hr = dev->OpenSharedResource(h, IID_PPV_ARGS(&tex));
    printf("OpenSharedResource -> 0x%08lx\n", hr);
    if (FAILED(hr)) {
        ComPtr<ID3D11Device1> dev1;
        dev.As(&dev1);
        hr = dev1->OpenSharedResource1(h, IID_PPV_ARGS(&tex));
        printf("OpenSharedResource1 -> 0x%08lx\n", hr);
        if (FAILED(hr)) return 1;
    }
    D3D11_TEXTURE2D_DESC td{};
    tex->GetDesc(&td);
    RECT wr{}, cr{}, er{};
    GetWindowRect(hwnd, &wr);
    GetClientRect(hwnd, &cr);
    DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &er, sizeof(er));
    POINT co{0, 0};
    ClientToScreen(hwnd, &co);
    printf("surface %ux%u fmt %u bind %x misc %x | GetWindowRect %ld,%ld %ldx%ld | ext frame %ld,%ld %ldx%ld | client %ldx%ld at %ld,%ld\n",
           td.Width, td.Height, td.Format, td.BindFlags, td.MiscFlags, wr.left, wr.top, wr.right - wr.left,
           wr.bottom - wr.top, er.left, er.top, er.right - er.left, er.bottom - er.top, cr.right, cr.bottom, co.x, co.y);

    // Watch the update id for 3 s (move the view in Blender meanwhile to see it tick).
    int ticks = 0;
    for (int i = 0; i < 30; ++i) {
        Sleep(100);
        HANDLE h2 = nullptr; LUID l2{}; ULONG f2 = 0, fl2 = 0; ULONGLONG u2 = 0;
        if (!fn(hwnd, &h2, &l2, &f2, &fl2, &u2)) { printf("  re-query failed gle=%lu\n", GetLastError()); break; }
        if (u2 != upd || h2 != h) { ++ticks; if (ticks <= 5) printf("  t=%4dms updateId %llu handle %p\n", i * 100, u2, h2); upd = u2; h = h2; }
    }
    printf("update ticks in 3 s: %d\n", ticks);

    td.Usage = D3D11_USAGE_STAGING;
    td.BindFlags = 0;
    td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    td.MiscFlags = 0;
    ComPtr<ID3D11Texture2D> st;
    hr = dev->CreateTexture2D(&td, nullptr, &st);
    if (FAILED(hr)) { printf("staging 0x%08lx\n", hr); return 1; }
    ctx->CopyResource(st.Get(), tex.Get());
    D3D11_MAPPED_SUBRESOURCE m{};
    hr = ctx->Map(st.Get(), 0, D3D11_MAP_READ, 0, &m);
    if (FAILED(hr)) { printf("map 0x%08lx\n", hr); return 1; }
    std::vector<unsigned char> px(static_cast<size_t>(td.Width) * td.Height * 4);
    for (UINT y = 0; y < td.Height; ++y) {
        const unsigned char* row = static_cast<const unsigned char*>(m.pData) + static_cast<size_t>(y) * m.RowPitch;
        for (UINT x = 0; x < td.Width; ++x) {
            unsigned char* o = &px[(static_cast<size_t>(y) * td.Width + x) * 4];
            o[0] = row[x * 4 + 2]; o[1] = row[x * 4 + 1]; o[2] = row[x * 4 + 0]; o[3] = 255;
        }
    }
    ctx->Unmap(st.Get(), 0);
    stbi_write_png("dwm_surface.png", static_cast<int>(td.Width), static_cast<int>(td.Height), 4, px.data(), static_cast<int>(td.Width * 4));
    printf("wrote dwm_surface.png (%ux%u)\n", td.Width, td.Height);
    return 0;
}
