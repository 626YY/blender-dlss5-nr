// live.cpp - DLSS 5 Neural Rendering live overlay (GPU route). See live.h.
#include "common.h"

#include <d3d11_4.h>
#include <dcomp.h>
#include <dwmapi.h>
#include <dxgi1_2.h>

// Windows.Graphics.Capture (per-window capture) via C++/WinRT.
#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <Windows.Graphics.Capture.Interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "archspoof.h"
#include "dlss.h"
#include "gpu.h"
#include "jsonmini.h"
#include "live.h"
#include "nr.h"
#include "optflow_nvof.h"
#include "pipeline.h"

#include "stb_image_write.h"

namespace {

// ---------------------------------------------------------------------------
// Configuration pushed from Blender over stdin.
// ---------------------------------------------------------------------------
struct Hole {
    int x = 0, y = 0, w = 0, h = 0;
};

struct Cfg {
    HWND hwnd = nullptr;
    int rect[4] = {0, 0, 0, 0};  // Blender client coords, top-left origin
    std::vector<Hole> holes;
    float split = 0.5f;
    int view = 0;                // 0 split, 1 NR only, 2 original only (hidden)
    bool half = true;
    NrModelParams model{};
    float detail = 1.0f;
    float colour = 0.0f;         // 0 keep original hue (COLOR mode), 1 model colour (FULL)
    bool bench = false;
    bool preferWgc = true;       // per-window capture (recorders see the overlay; Win10 draws a yellow border)
    std::string dump;            // debug: write the next presented frame to this PNG path
    unsigned gen = 0;            // any change -> re-present
    unsigned modelGen = 0;       // model params changed -> re-create the feature
};

struct Stats {
    std::atomic<double> fps{0.0}, ms{0.0}, nrMs{0.0}, capMs{0.0};
    std::atomic<long long> frames{0};
    std::atomic<bool> visible{false};
    std::atomic<bool> running{true};
    std::mutex mu;
    std::string err;
    void SetErr(const std::string& e) { std::lock_guard<std::mutex> g(mu); err = e; }
    std::string Err() { std::lock_guard<std::mutex> g(mu); return err; }
};

// ---------------------------------------------------------------------------
// Forwarder (nvngx.dll_dlssnr.dll next to the exe) - the same C API nr.cpp drives.
// ---------------------------------------------------------------------------
struct Fwd {
    HMODULE mod = nullptr;
    std::wstring path;
    void* (*create)(const wchar_t*, const wchar_t*, ID3D12Device*, ID3D12GraphicsCommandList*,
                    NVSDK_NGX_Parameter*, unsigned, unsigned, unsigned, unsigned,
                    const NrModelParams*) = nullptr;
    int (*evaluate)(ID3D12GraphicsCommandList*, void*, NVSDK_NGX_Parameter*, ID3D12Resource*,
                    ID3D12Resource*, ID3D12Resource*, ID3D12Resource*, unsigned, unsigned, unsigned,
                    unsigned, int) = nullptr;
    void (*release)(void*) = nullptr;
    const int* excStage = nullptr;

    bool Load() {
        wchar_t exePath[MAX_PATH]{};
        GetModuleFileNameW(nullptr, exePath, MAX_PATH);
        std::wstring p = exePath;
        const size_t s = p.find_last_of(L"\\/");
        p = (s == std::wstring::npos ? std::wstring() : p.substr(0, s + 1)) + L"nvngx.dll_dlssnr.dll";
        path = p;
        mod = LoadLibraryExW(p.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (!mod) return false;
        create = reinterpret_cast<decltype(create)>(GetProcAddress(mod, "fwd_create"));
        evaluate = reinterpret_cast<decltype(evaluate)>(GetProcAddress(mod, "fwd_evaluate"));
        release = reinterpret_cast<decltype(release)>(GetProcAddress(mod, "fwd_release"));
        excStage = reinterpret_cast<const int*>(GetProcAddress(mod, "fwd_last_exception_stage"));
        return create && evaluate && release;
    }
    bool Crashed() const { return excStage && *excStage != 0; }
};

std::wstring SnippetPath(const std::string& dllDir) {
    std::wstring p = Narrow2Widen(dllDir.empty() ? std::string("nvngx_dlssnr.dll")
                                                 : dllDir + "\\nvngx_dlssnr.dll");
    wchar_t abs[MAX_PATH]{};
    if (GetFullPathNameW(p.c_str(), MAX_PATH, abs, nullptr)) p = abs;
    return p;
}

std::wstring DataPath() {
    wchar_t appData[MAX_PATH]{};
    const DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", appData, MAX_PATH);
    std::wstring p = (n > 0 && n < MAX_PATH) ? std::wstring(appData) + L"\\dlss5_live" : std::wstring(L".");
    CreateDirectoryW(p.c_str(), nullptr);
    return p;
}

// ---------------------------------------------------------------------------
// stdin control thread. One JSON object per line.
// ---------------------------------------------------------------------------
std::string StatusJson(Stats& st) {
    char buf[512];
    snprintf(buf, sizeof(buf),
             "{\"ok\":1,\"fps\":%.2f,\"ms\":%.1f,\"nr_ms\":%.1f,\"cap_ms\":%.1f,\"frames\":%lld,"
             "\"visible\":%s,\"running\":%s,\"err\":\"%s\",\"route\":\"gpu\"}",
             st.fps.load(), st.ms.load(), st.nrMs.load(), st.capMs.load(), st.frames.load(),
             st.visible.load() ? "true" : "false", st.running.load() ? "true" : "false",
             JEscape(st.Err()).c_str());
    return buf;
}

void Reply(const std::string& line) {
    std::fputs(line.c_str(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

void ApplyCfg(const JVal& j, Cfg& c) {
    if (j.has("hwnd")) c.hwnd = reinterpret_cast<HWND>(static_cast<uintptr_t>(j.num("hwnd", 0)));
    if (const JVal* r = j.get("rect"); r && r->kind == JVal::Arr && r->a.size() >= 4) {
        for (int i = 0; i < 4; ++i) c.rect[i] = static_cast<int>(r->a[i].n);
    }
    if (const JVal* hs = j.get("holes"); hs && hs->kind == JVal::Arr) {
        c.holes.clear();
        for (const JVal& h : hs->a) {
            if (h.kind == JVal::Arr && h.a.size() >= 4) {
                c.holes.push_back({static_cast<int>(h.a[0].n), static_cast<int>(h.a[1].n),
                                   static_cast<int>(h.a[2].n), static_cast<int>(h.a[3].n)});
            }
        }
    }
    c.split = static_cast<float>(j.num("split", c.split));
    if (j.has("view")) {
        const std::string v = j.str("view", "SPLIT");
        c.view = (v == "NR") ? 1 : (v == "ORIG") ? 2 : 0;
    }
    c.half = j.boolean("half", c.half);
    c.bench = j.boolean("bench", c.bench);
    if (j.has("capture")) c.preferWgc = (j.str("capture", "wgc") != "dda");
    if (j.has("dump")) c.dump = j.str("dump", "");
    // composition
    if (j.has("mode")) {
        const std::string m = j.str("mode", "COLOR");
        c.colour = (m == "FULL") ? 1.0f : 0.0f;
    }
    c.detail = static_cast<float>(j.num("strength", c.detail));
    // model (latched at create)
    NrModelParams m = c.model;
    m.preset = 0;
    m.intensity = 1.0f;
    m.style = static_cast<unsigned>(j.num("style", m.style));
    m.localTone = static_cast<float>(j.num("tone", m.localTone));
    m.localStructure = static_cast<float>(j.num("structure", m.localStructure));
    m.skinStructure = static_cast<float>(j.num("skin", m.skinStructure));
    m.autoMask = j.boolean("automask", m.autoMask != 0) ? 1u : 0u;
    m.uiCorrection = 0;
    if (std::memcmp(&m, &c.model, sizeof(m)) != 0) {
        c.model = m;
        ++c.modelGen;
    }
    ++c.gen;
}

void StdinLoop(Cfg* cfg, std::mutex* mu, std::atomic<bool>* quit, Stats* stats) {
    std::string line;
    while (!quit->load() && std::getline(std::cin, line)) {
        if (line.empty()) continue;
        JVal j;
        if (!JParser::Parse(line, &j) || j.kind != JVal::Obj) {
            Reply("{\"ok\":0,\"err\":\"bad json\"}");
            continue;
        }
        const std::string cmd = j.str("cmd", "");
        if (cmd == "quit" || cmd == "stop") {
            quit->store(true);
            Reply("{\"ok\":1}");
            break;
        }
        if (cmd == "start" || cmd == "update") {
            {
                std::lock_guard<std::mutex> g(*mu);
                const JVal* c = j.get("cfg");
                ApplyCfg(c && c->kind == JVal::Obj ? *c : j, *cfg);
            }
            Reply(StatusJson(*stats));
            continue;
        }
        Reply(StatusJson(*stats));  // status / anything else
    }
    quit->store(true);
}

// ---------------------------------------------------------------------------
// Desktop Duplication capture (D3D11) -> shared texture the D3D12 side reads.
// ---------------------------------------------------------------------------
class Capture {
public:
    bool Init(IDXGIAdapter1* adapter, ID3D12Device* dev12, std::string* err) {
        D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
        HRESULT hr = D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr,
                                       D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels, 2, D3D11_SDK_VERSION,
                                       &m_dev, nullptr, &m_ctx);
        if (FAILED(hr)) { *err = "D3D11CreateDevice: " + HrToString(hr); return false; }
        if (FAILED(m_dev.As(&m_dev5)) || FAILED(m_ctx.As(&m_ctx4))) {
            *err = "D3D11.4 (fences) unavailable";
            return false;
        }
        hr = m_dev5->CreateFence(0, D3D11_FENCE_FLAG_SHARED, IID_PPV_ARGS(&m_fence11));
        if (FAILED(hr)) { *err = "CreateFence(shared): " + HrToString(hr); return false; }
        HANDLE fh = nullptr;
        hr = m_fence11->CreateSharedHandle(nullptr, GENERIC_ALL, nullptr, &fh);
        if (FAILED(hr)) { *err = "fence CreateSharedHandle: " + HrToString(hr); return false; }
        hr = dev12->OpenSharedHandle(fh, IID_PPV_ARGS(&m_fence12));
        CloseHandle(fh);
        if (FAILED(hr)) { *err = "OpenSharedHandle(fence): " + HrToString(hr); return false; }
        m_adapter = adapter;
        m_dev12 = dev12;
        return true;
    }

    // (Re)duplicate the output that shows `mon`.
    bool SelectOutput(HMONITOR mon, std::string* err) {
        m_dup.Reset();
        for (UINT i = 0;; ++i) {
            ComPtr<IDXGIOutput> out;
            if (m_adapter->EnumOutputs(i, &out) == DXGI_ERROR_NOT_FOUND) break;
            DXGI_OUTPUT_DESC d{};
            out->GetDesc(&d);
            if (d.Monitor != mon) continue;
            ComPtr<IDXGIOutput1> out1;
            if (FAILED(out.As(&out1))) { *err = "IDXGIOutput1 unavailable"; return false; }
            HRESULT hr = out1->DuplicateOutput(m_dev.Get(), &m_dup);
            if (FAILED(hr)) { *err = "DuplicateOutput: " + HrToString(hr); return false; }
            m_outRect = d.DesktopCoordinates;
            m_mon = mon;
            m_meta.resize(1 << 16);
            return true;
        }
        *err = "no DXGI output for this monitor on the NR adapter";
        return false;
    }
    HMONITOR Monitor() const { return m_mon; }

    // 1 = new frame copied into the shared texture, 0 = nothing new for this rect, -1 = lost.
    int Acquire(const RECT& screenRect, bool force, std::string* err) {
        if (!m_dup) return -1;
        const int w = screenRect.right - screenRect.left, h = screenRect.bottom - screenRect.top;
        if (!EnsureShared(w, h, err)) return -1;
        DXGI_OUTDUPL_FRAME_INFO info{};
        ComPtr<IDXGIResource> res;
        HRESULT hr = m_dup->AcquireNextFrame(force ? 0 : 8, &info, &res);
        if (hr == DXGI_ERROR_WAIT_TIMEOUT) return 0;
        if (hr == DXGI_ERROR_ACCESS_LOST || hr == DXGI_ERROR_INVALID_CALL) return -1;
        if (FAILED(hr)) { *err = "AcquireNextFrame: " + HrToString(hr); return -1; }
        bool changed = force || info.AccumulatedFrames > 0 || info.LastPresentTime.QuadPart != 0;
        // Local (output) coordinates of our rect.
        RECT local{screenRect.left - m_outRect.left, screenRect.top - m_outRect.top,
                   screenRect.right - m_outRect.left, screenRect.bottom - m_outRect.top};
        if (changed && !force && info.TotalMetadataBufferSize > 0) {
            if (m_meta.size() < info.TotalMetadataBufferSize) m_meta.resize(info.TotalMetadataBufferSize);
            UINT need = 0;
            bool hit = false;
            if (SUCCEEDED(m_dup->GetFrameMoveRects(static_cast<UINT>(m_meta.size()),
                                                   reinterpret_cast<DXGI_OUTDUPL_MOVE_RECT*>(m_meta.data()),
                                                   &need))) {
                const auto* mv = reinterpret_cast<DXGI_OUTDUPL_MOVE_RECT*>(m_meta.data());
                for (UINT i = 0; i < need / sizeof(DXGI_OUTDUPL_MOVE_RECT); ++i) {
                    if (Intersects(mv[i].DestinationRect, local)) { hit = true; break; }
                }
            }
            if (!hit && SUCCEEDED(m_dup->GetFrameDirtyRects(static_cast<UINT>(m_meta.size()),
                                                             reinterpret_cast<RECT*>(m_meta.data()),
                                                             &need))) {
                const auto* dr = reinterpret_cast<RECT*>(m_meta.data());
                for (UINT i = 0; i < need / sizeof(RECT); ++i) {
                    if (Intersects(dr[i], local)) { hit = true; break; }
                }
            }
            changed = hit;
        }
        int result = 0;
        if (changed) {
            ComPtr<ID3D11Texture2D> desk;
            if (SUCCEEDED(res.As(&desk))) {
                const int ow = m_outRect.right - m_outRect.left, oh = m_outRect.bottom - m_outRect.top;
                D3D11_BOX box{};
                box.left = static_cast<UINT>(std::max<LONG>(0, local.left));
                box.top = static_cast<UINT>(std::max<LONG>(0, local.top));
                box.right = static_cast<UINT>(std::min<LONG>(ow, local.right));
                box.bottom = static_cast<UINT>(std::min<LONG>(oh, local.bottom));
                box.front = 0;
                box.back = 1;
                if (box.right > box.left && box.bottom > box.top) {
                    m_ctx->CopySubresourceRegion(m_shared.Get(), 0, 0, 0, 0, desk.Get(), 0, &box);
                    m_ctx->Flush();
                    m_ctx4->Signal(m_fence11.Get(), ++m_fenceVal);
                    result = 1;
                }
            }
        }
        m_dup->ReleaseFrame();
        return result;
    }

    ID3D12Resource* Shared12() const { return m_shared12.Get(); }
    ID3D12Fence* Fence12() const { return m_fence12.Get(); }
    UINT64 FenceValue() const { return m_fenceVal; }
    int Width() const { return m_w; }
    int Height() const { return m_h; }

    // ---- Windows.Graphics.Capture: capture one window (Blender) regardless of what is on top ----
    bool Wgc() const { return m_wgc; }
    bool StartWgc(HWND hwnd, std::string* err) {
        namespace wgc = winrt::Windows::Graphics::Capture;
        namespace wdx = winrt::Windows::Graphics::DirectX;
        StopWgc();
        try {
            auto interop = winrt::get_activation_factory<wgc::GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
            wgc::GraphicsCaptureItem item{nullptr};
            winrt::check_hresult(interop->CreateForWindow(hwnd, winrt::guid_of<wgc::GraphicsCaptureItem>(),
                                                          winrt::put_abi(item)));
            if (!m_winrtDevice) {
                ComPtr<IDXGIDevice> dxgi;
                CHECK_HR(m_dev.As(&dxgi));
                winrt::com_ptr<IInspectable> insp;
                winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi.Get(), insp.put()));
                m_winrtDevice = insp.as<wdx::Direct3D11::IDirect3DDevice>();
            }
            m_poolSize = item.Size();
            m_pool = wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(
                m_winrtDevice, wdx::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, m_poolSize);
            m_session = m_pool.CreateCaptureSession(item);
            try { m_session.IsCursorCaptureEnabled(false); } catch (...) {}
            // The yellow capture border: Windows 11 lets an unpackaged app ask for borderless capture;
            // Windows 10 (up to 22H2) does not, so the border stays there.
            try {
                wgc::GraphicsCaptureAccess::RequestAccessAsync(wgc::GraphicsCaptureAccessKind::Borderless).get();
            } catch (...) {}
            try { m_session.IsBorderRequired(false); } catch (...) { LogWarn("WGC: this Windows build keeps the yellow capture border"); }
            m_session.StartCapture();
            m_item = item;
            m_wgcHwnd = hwnd;
            m_wgc = true;
            return true;
        } catch (const winrt::hresult_error& e) {
            *err = "WGC: " + Widen2Narrow(std::wstring(e.message()));
        } catch (const std::exception& e) {
            *err = std::string("WGC: ") + e.what();
        }
        StopWgc();
        return false;
    }
    void StopWgc() {
        try {
            if (m_session) m_session.Close();
            if (m_pool) m_pool.Close();
        } catch (...) {}
        m_session = nullptr;
        m_pool = nullptr;
        m_item = nullptr;
        m_wgc = false;
        m_wgcHwnd = nullptr;
    }
    // l,t,w,h are Blender client coordinates. 1 = copied, 0 = no new frame, -1 = lost.
    int AcquireWgc(HWND hwnd, int l, int t, int w, int h, bool force, std::string* err) {
        namespace wdx = winrt::Windows::Graphics::DirectX;
        if (!m_wgc || hwnd != m_wgcHwnd) return -1;
        if (!EnsureShared(w, h, err)) return -1;
        try {
            auto frame = m_pool.TryGetNextFrame();
            if (!frame) {
                if (force && m_haveFrame) {          // bench: reuse the last copy as if it were new
                    m_ctx4->Signal(m_fence11.Get(), ++m_fenceVal);
                    return 1;
                }
                return 0;
            }
            auto size = frame.ContentSize();
            if (size.Width != m_poolSize.Width || size.Height != m_poolSize.Height) {
                m_poolSize = size;
                m_pool.Recreate(m_winrtDevice, wdx::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, size);
                frame.Close();
                return 0;
            }
            auto access = frame.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
            winrt::com_ptr<ID3D11Texture2D> tex;
            winrt::check_hresult(access->GetInterface(winrt::guid_of<ID3D11Texture2D>(), tex.put_void()));
            // The captured image starts at the window's visible frame bounds; map client coords into it.
            RECT wr{};
            if (FAILED(DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &wr, sizeof(wr)))) GetWindowRect(hwnd, &wr);
            POINT co{0, 0};
            ClientToScreen(hwnd, &co);
            const int ox = co.x - wr.left + l, oy = co.y - wr.top + t;
            D3D11_BOX box{};
            box.left = static_cast<UINT>(std::max(0, ox));
            box.top = static_cast<UINT>(std::max(0, oy));
            box.right = static_cast<UINT>(std::min<int>(size.Width, ox + w));
            box.bottom = static_cast<UINT>(std::min<int>(size.Height, oy + h));
            box.front = 0;
            box.back = 1;
            int result = 0;
            if (box.right > box.left && box.bottom > box.top) {
                m_ctx->CopySubresourceRegion(m_shared.Get(), 0, 0, 0, 0, tex.get(), 0, &box);
                m_ctx->Flush();
                m_ctx4->Signal(m_fence11.Get(), ++m_fenceVal);
                m_haveFrame = true;
                result = 1;
            }
            frame.Close();
            return result;
        } catch (const winrt::hresult_error& e) {
            *err = "WGC frame: " + Widen2Narrow(std::wstring(e.message()));
            return -1;
        }
    }

private:
    winrt::Windows::Graphics::Capture::GraphicsCaptureItem m_item{nullptr};
    winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool m_pool{nullptr};
    winrt::Windows::Graphics::Capture::GraphicsCaptureSession m_session{nullptr};
    winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice m_winrtDevice{nullptr};
    winrt::Windows::Graphics::SizeInt32 m_poolSize{};
    HWND m_wgcHwnd = nullptr;
    bool m_wgc = false;
    bool m_haveFrame = false;
    static bool Intersects(const RECT& a, const RECT& b) {
        return !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom);
    }
    bool EnsureShared(int w, int h, std::string* err) {
        if (m_shared && m_w == w && m_h == h) return true;
        m_shared12.Reset();
        m_shared.Reset();
        D3D11_TEXTURE2D_DESC td{};
        td.Width = static_cast<UINT>(w);
        td.Height = static_cast<UINT>(h);
        td.MipLevels = 1;
        td.ArraySize = 1;
        td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        td.MiscFlags = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
        HRESULT hr = m_dev->CreateTexture2D(&td, nullptr, &m_shared);
        if (FAILED(hr)) { *err = "shared texture: " + HrToString(hr); return false; }
        ComPtr<IDXGIResource1> r1;
        if (FAILED(m_shared.As(&r1))) { *err = "IDXGIResource1"; return false; }
        HANDLE hnd = nullptr;
        hr = r1->CreateSharedHandle(nullptr, DXGI_SHARED_RESOURCE_READ | DXGI_SHARED_RESOURCE_WRITE,
                                    nullptr, &hnd);
        if (FAILED(hr)) { *err = "CreateSharedHandle(tex): " + HrToString(hr); return false; }
        hr = m_dev12->OpenSharedHandle(hnd, IID_PPV_ARGS(&m_shared12));
        CloseHandle(hnd);
        if (FAILED(hr)) { *err = "OpenSharedHandle(tex): " + HrToString(hr); return false; }
        m_w = w;
        m_h = h;
        return true;
    }

    ComPtr<ID3D11Device> m_dev;
    ComPtr<ID3D11Device5> m_dev5;
    ComPtr<ID3D11DeviceContext> m_ctx;
    ComPtr<ID3D11DeviceContext4> m_ctx4;
    ComPtr<ID3D11Fence> m_fence11;
    ComPtr<ID3D12Fence> m_fence12;
    UINT64 m_fenceVal = 0;
    ComPtr<IDXGIOutputDuplication> m_dup;
    RECT m_outRect{};
    HMONITOR m_mon = nullptr;
    std::vector<uint8_t> m_meta;
    ComPtr<ID3D11Texture2D> m_shared;
    ComPtr<ID3D12Resource> m_shared12;
    IDXGIAdapter1* m_adapter = nullptr;
    ID3D12Device* m_dev12 = nullptr;
    int m_w = 0, m_h = 0;
};

// ---------------------------------------------------------------------------
// Our own compute passes: decode (capture -> linear, optional 2x down), present (final -> swap
// chain, split / holes / alpha), thumb (32x32 luma of the capture for change detection).
// ---------------------------------------------------------------------------
static const char* kLiveHlsl = R"HLSL(
cbuffer C : register(b0) {
    uint2 gOut; uint gScale; uint gMode;      // mode: decode 0 / present 1 / thumb 2
    uint gSplitX; uint gView; uint gGain; uint gNumHoles;
    int4 gHoles[6];
};
Texture2D<float4>   gA : register(t0);   // decode/thumb/present: captured BGRA8 (sRGB coded)
Texture2D<float4>   gB : register(t1);   // present: composite (RGBA8 sRGB) at NR res
RWTexture2D<float4> gDst : register(u0);
float3 SrgbToLin(float3 c) {
    c = max(c, 0.0f);
    float3 lo = c / 12.92f;
    float3 hi = pow((c + 0.055f) / 1.055f, 2.4f);
    return lerp(lo, hi, step(0.04045f, c));
}
float3 LinToSrgb(float3 c) {
    c = max(c, 0.0f);
    float3 lo = c * 12.92f;
    float3 hi = 1.055f * pow(max(c, 1e-8f), 1.0f / 2.4f) - 0.055f;
    return lerp(hi, lo, step(c, 0.0031308f));
}
float Luma(float3 c) { return dot(c, float3(0.2126f, 0.7152f, 0.0722f)); }
float3 CapLinAvg(uint2 p, uint s) {   // average of the s x s captured block at p*s, in linear
    float3 acc = 0;
    for (uint y = 0; y < s; ++y)
        for (uint x = 0; x < s; ++x)
            acc += SrgbToLin(gA[p * s + uint2(x, y)].rgb);
    return acc / (s * s);
}
float3 Bilinear(Texture2D<float4> t, float2 uvPix, uint2 size) {   // manual bilinear, sRGB in
    float2 f = uvPix - 0.5f;
    int2 i0 = int2(floor(f));
    float2 w = f - i0;
    int2 mx = int2(size) - 1;
    int2 a = clamp(i0, int2(0, 0), mx), b = clamp(i0 + int2(1, 0), int2(0, 0), mx);
    int2 c = clamp(i0 + int2(0, 1), int2(0, 0), mx), d = clamp(i0 + int2(1, 1), int2(0, 0), mx);
    float3 top = lerp(t[a].rgb, t[b].rgb, w.x);
    float3 bot = lerp(t[c].rgb, t[d].rgb, w.x);
    return lerp(top, bot, w.y);
}
[numthreads(8, 8, 1)]
void CSMain(uint3 t : SV_DispatchThreadID) {
    if (t.x >= gOut.x || t.y >= gOut.y) return;
    if (gMode == 0) {                        // decode: captured -> linear RGBA16F at NR res
        gDst[t.xy] = float4(CapLinAvg(t.xy, gScale), 1.0f);
        return;
    }
    if (gMode == 2) {                        // thumb: 32x32 mean luma of the capture
        uint2 cap; gA.GetDimensions(cap.x, cap.y);
        uint2 bs = max(cap / 32, uint2(1, 1));
        float acc = 0;
        for (uint y = 0; y < bs.y; y += max(bs.y / 4, 1))
            for (uint x = 0; x < bs.x; x += max(bs.x / 4, 1))
                acc += Luma(gA[t.xy * bs + uint2(x, y)].rgb);
        gDst[t.xy] = float4(acc, 0, 0, 1);
        return;
    }
    // present
    bool onLine = (gView == 0) && (t.x + 1 >= gSplitX) && (t.x < gSplitX + 1);
    bool nrSide = (gView == 1) || (gView == 0 && t.x >= gSplitX);
    for (uint i = 0; i < gNumHoles; ++i) {
        int4 h = gHoles[i];
        if ((int) t.x >= h.x && (int) t.x < h.x + h.z && (int) t.y >= h.y && (int) t.y < h.y + h.w) { nrSide = false; onLine = false; }
    }
    if (onLine) { gDst[t.xy] = float4(1.0f, 0.82f, 0.12f, 1.0f); return; }
    if (!nrSide) { gDst[t.xy] = float4(0, 0, 0, 0); return; }   // premultiplied transparent
    uint2 nrSize; gB.GetDimensions(nrSize.x, nrSize.y);
    if (gScale == 1) {
        gDst[t.xy] = float4(saturate(gB[min(t.xy, nrSize - 1)].rgb), 1.0f);
        return;
    }
    float2 uvPix = (float2(t.xy) + 0.5f) / gScale;
    float3 fin = Bilinear(gB, uvPix, nrSize);                    // composite, sRGB
    if (gGain == 0) { gDst[t.xy] = float4(saturate(fin), 1.0f); return; }
    // luminance-gain upsample: keep the full-res original, apply the model's luminance change
    float3 origLin = SrgbToLin(gA[t.xy].rgb);
    uint2 blk = min(t.xy / gScale, nrSize - 1);
    float lo = Luma(CapLinAvg(blk, gScale));
    float lf = Luma(SrgbToLin(fin));
    float gain = clamp(lf / max(lo, 1e-4f), 0.0f, 4.0f);
    gDst[t.xy] = float4(saturate(LinToSrgb(origLin * gain)), 1.0f);
}
)HLSL";

struct LiveConsts {
    uint32_t out[2];
    uint32_t scale;
    uint32_t mode;
    uint32_t splitX;
    uint32_t view;
    uint32_t gain;
    uint32_t numHoles;
    int32_t holes[6][4];
};
static_assert(sizeof(LiveConsts) == 32 * 4, "root constants: 32 dwords");

class LivePasses {
public:
    void Init(ID3D12Device* dev) {
        m_dev = dev;
        D3D12_DESCRIPTOR_RANGE ranges[2]{};
        ranges[0].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
        ranges[0].NumDescriptors = 2;
        ranges[0].OffsetInDescriptorsFromTableStart = D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND;
        ranges[1].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
        ranges[1].NumDescriptors = 1;
        ranges[1].OffsetInDescriptorsFromTableStart = D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND;
        D3D12_ROOT_PARAMETER params[2]{};
        params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
        params[0].Constants.Num32BitValues = sizeof(LiveConsts) / 4;
        params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
        params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        params[1].DescriptorTable.NumDescriptorRanges = 2;
        params[1].DescriptorTable.pDescriptorRanges = ranges;
        params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
        D3D12_ROOT_SIGNATURE_DESC rs{};
        rs.NumParameters = 2;
        rs.pParameters = params;
        ComPtr<ID3DBlob> sig, err;
        HRESULT hr = D3D12SerializeRootSignature(&rs, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err);
        if (FAILED(hr)) throw ToolError("live root signature: " + HrToString(hr));
        CHECK_HR(dev->CreateRootSignature(0, sig->GetBufferPointer(), sig->GetBufferSize(), IID_PPV_ARGS(&m_sig)));
        ComPtr<ID3DBlob> cs, csErr;
        hr = D3DCompile(kLiveHlsl, strlen(kLiveHlsl), "live.hlsl", nullptr, nullptr, "CSMain", "cs_5_0",
                        D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &cs, &csErr);
        if (FAILED(hr)) {
            throw ToolError(std::string("live.hlsl: ") +
                            (csErr ? static_cast<const char*>(csErr->GetBufferPointer()) : HrToString(hr)));
        }
        D3D12_COMPUTE_PIPELINE_STATE_DESC pd{};
        pd.pRootSignature = m_sig.Get();
        pd.CS.pShaderBytecode = cs->GetBufferPointer();
        pd.CS.BytecodeLength = cs->GetBufferSize();
        CHECK_HR(dev->CreateComputePipelineState(&pd, IID_PPV_ARGS(&m_pso)));
        D3D12_DESCRIPTOR_HEAP_DESC hd{};
        hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
        hd.NumDescriptors = 96;
        hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
        CHECK_HR(dev->CreateDescriptorHeap(&hd, IID_PPV_ARGS(&m_heap)));
        m_descSize = dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    }
    void BeginFrame() { m_next = 0; }

    void Record(ID3D12GraphicsCommandList* cl, ID3D12Resource* srv0, DXGI_FORMAT f0,
                ID3D12Resource* srv1, DXGI_FORMAT f1, ID3D12Resource* uav, DXGI_FORMAT fu,
                const LiveConsts& c) {
        if (m_next + 3 > 96) throw ToolError("live descriptor heap exhausted");
        const int base = m_next;
        m_next += 3;
        D3D12_SHADER_RESOURCE_VIEW_DESC sd{};
        sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        sd.Texture2D.MipLevels = 1;
        sd.Format = f0;
        m_dev->CreateShaderResourceView(srv0, &sd, Cpu(base));
        sd.Format = f1;
        m_dev->CreateShaderResourceView(srv1, &sd, Cpu(base + 1));
        D3D12_UNORDERED_ACCESS_VIEW_DESC ud{};
        ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
        ud.Format = fu;
        m_dev->CreateUnorderedAccessView(uav, nullptr, &ud, Cpu(base + 2));
        ID3D12DescriptorHeap* heaps[] = {m_heap.Get()};
        cl->SetDescriptorHeaps(1, heaps);
        cl->SetPipelineState(m_pso.Get());
        cl->SetComputeRootSignature(m_sig.Get());
        cl->SetComputeRoot32BitConstants(0, sizeof(c) / 4, &c, 0);
        cl->SetComputeRootDescriptorTable(1, Gpu(base));
        cl->Dispatch((c.out[0] + 7) / 8, (c.out[1] + 7) / 8, 1);
    }

private:
    D3D12_CPU_DESCRIPTOR_HANDLE Cpu(int i) const {
        auto h = m_heap->GetCPUDescriptorHandleForHeapStart();
        h.ptr += static_cast<SIZE_T>(i) * m_descSize;
        return h;
    }
    D3D12_GPU_DESCRIPTOR_HANDLE Gpu(int i) const {
        auto h = m_heap->GetGPUDescriptorHandleForHeapStart();
        h.ptr += static_cast<UINT64>(i) * m_descSize;
        return h;
    }
    ID3D12Device* m_dev = nullptr;
    ComPtr<ID3D12RootSignature> m_sig;
    ComPtr<ID3D12PipelineState> m_pso;
    ComPtr<ID3D12DescriptorHeap> m_heap;
    UINT m_descSize = 0;
    int m_next = 0;
};

void Barrier(ID3D12GraphicsCommandList* cl, ID3D12Resource* r, D3D12_RESOURCE_STATES from,
             D3D12_RESOURCE_STATES to) {
    if (from == to) return;
    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = r;
    b.Transition.StateBefore = from;
    b.Transition.StateAfter = to;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    cl->ResourceBarrier(1, &b);
}

// ---------------------------------------------------------------------------
// The overlay window: click-through popup owned by the Blender window, DirectComposition swap chain.
// ---------------------------------------------------------------------------
constexpr DWORD kWdaExcludeFromCapture = 0x11;

class Overlay {
public:
    // The overlay is an *owned* popup of the Blender window: Windows keeps it directly above its
    // owner in z-order, hides it when the owner is minimised, and puts it behind whatever covers
    // Blender. So it never leaks over other applications, and no foreground heuristics are needed.
    bool Create(HWND owner, std::string* err) {
        if (m_hwnd && owner == m_owner) return true;
        Destroy();
        WNDCLASSW wc{};
        wc.lpfnWndProc = DefWindowProcW;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpszClassName = L"DLSS5_Live_Overlay";
        RegisterClassW(&wc);
        const DWORD ex = WS_EX_NOREDIRECTIONBITMAP | WS_EX_LAYERED | WS_EX_TRANSPARENT |
                         WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
        m_hwnd = CreateWindowExW(ex, L"DLSS5_Live_Overlay", L"DLSS 5 NR (GPU)", WS_POPUP, 0, 0, 8, 8,
                                 owner, nullptr, wc.hInstance, nullptr);
        if (!m_hwnd) { *err = "CreateWindowExW failed: " + std::to_string(GetLastError()); return false; }
        m_owner = owner;
        SetLayeredWindowAttributes(m_hwnd, 0, 255, LWA_ALPHA);
        return true;
    }
    HWND Owner() const { return m_owner; }
    bool Visible() const { return m_hwnd && IsWindowVisible(m_hwnd) != 0; }
    // Only needed with desktop duplication (otherwise the overlay would capture itself). With
    // per-window capture the overlay stays visible to screen recorders.
    bool SetExcludeFromCapture(bool on) {
        m_affinityOk = SetWindowDisplayAffinity(m_hwnd, on ? kWdaExcludeFromCapture : 0) != 0;
        return m_affinityOk;
    }
    bool AffinityOk() const { return m_affinityOk; }

    bool EnsureSwapChain(IDXGIFactory6* factory, ID3D12CommandQueue* queue, int w, int h, std::string* err) {
        if (m_sc && m_w == w && m_h == h) return true;
        m_back[0].Reset();
        m_back[1].Reset();
        HRESULT hr;
        if (m_sc) {
            hr = m_sc->ResizeBuffers(2, static_cast<UINT>(w), static_cast<UINT>(h), DXGI_FORMAT_R8G8B8A8_UNORM,
                                     0);
            if (FAILED(hr)) { *err = "ResizeBuffers: " + HrToString(hr); return false; }
        } else {
            DXGI_SWAP_CHAIN_DESC1 sd{};
            sd.Width = static_cast<UINT>(w);
            sd.Height = static_cast<UINT>(h);
            sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
            sd.SampleDesc.Count = 1;
            sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;   // UAV usage is rejected on composition chains
            sd.BufferCount = 2;
            sd.Scaling = DXGI_SCALING_STRETCH;
            sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
            sd.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED;
            ComPtr<IDXGISwapChain1> sc1;
            hr = factory->CreateSwapChainForComposition(queue, &sd, nullptr, &sc1);
            if (FAILED(hr)) { *err = "CreateSwapChainForComposition: " + HrToString(hr); return false; }
            if (FAILED(sc1.As(&m_sc))) { *err = "IDXGISwapChain3"; return false; }
            hr = DCompositionCreateDevice2(nullptr, IID_PPV_ARGS(&m_dcomp));
            if (FAILED(hr)) { *err = "DCompositionCreateDevice2: " + HrToString(hr); return false; }
            hr = m_dcomp->CreateTargetForHwnd(m_hwnd, TRUE, &m_target);
            if (FAILED(hr)) { *err = "CreateTargetForHwnd: " + HrToString(hr); return false; }
            hr = m_dcomp->CreateVisual(&m_visual);
            if (FAILED(hr)) { *err = "CreateVisual: " + HrToString(hr); return false; }
            m_visual->SetContent(m_sc.Get());
            m_target->SetRoot(m_visual.Get());
            m_dcomp->Commit();
        }
        for (UINT i = 0; i < 2; ++i) {
            hr = m_sc->GetBuffer(i, IID_PPV_ARGS(&m_back[i]));
            if (FAILED(hr)) { *err = "GetBuffer: " + HrToString(hr); return false; }
        }
        m_w = w;
        m_h = h;
        return true;
    }
    bool HasSwapChain() const { return m_sc != nullptr; }
    ID3D12Resource* CurrentBackBuffer() const { return m_back[m_sc->GetCurrentBackBufferIndex()].Get(); }
    void Present() { m_sc->Present(1, 0); }

    void Place(int x, int y, int w, int h, bool show) {
        SetWindowPos(m_hwnd, nullptr, x, y, w, h,
                     SWP_NOACTIVATE | SWP_NOZORDER | (show ? SWP_SHOWWINDOW : SWP_HIDEWINDOW));
        m_shown = show;
    }
    void Hide() {
        if (m_shown) ShowWindow(m_hwnd, SW_HIDE);
        m_shown = false;
    }
    bool Shown() const { return m_shown; }
    HWND Hwnd() const { return m_hwnd; }
    void Pump() {
        MSG msg;
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
    void Destroy() {
        m_back[0].Reset();
        m_back[1].Reset();
        m_visual.Reset();
        m_target.Reset();
        m_sc.Reset();
        m_dcomp.Reset();
        if (m_hwnd) DestroyWindow(m_hwnd);
        m_hwnd = nullptr;
        m_owner = nullptr;
        m_w = m_h = 0;
        m_shown = false;
    }

private:
    HWND m_hwnd = nullptr;
    HWND m_owner = nullptr;
    ComPtr<IDXGISwapChain3> m_sc;
    ComPtr<ID3D12Resource> m_back[2];
    ComPtr<IDCompositionDevice> m_dcomp;
    ComPtr<IDCompositionTarget> m_target;
    ComPtr<IDCompositionVisual> m_visual;
    int m_w = 0, m_h = 0;
    bool m_shown = false;
    bool m_affinityOk = false;
};

}  // namespace

// ---------------------------------------------------------------------------
int RunLive(const LiveArgs& args) {
    SetLogToStderr(true);
    SetVerbose(args.verbose);
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    try {
        winrt::init_apartment(winrt::apartment_type::multi_threaded);
    } catch (...) {
        // already initialised by someone else in this thread: fine
    }

    Cfg cfg;
    cfg.model.style = 2;
    cfg.model.localTone = 0.0f;
    cfg.model.localStructure = 2.0f;
    cfg.model.skinStructure = 1.5f;
    cfg.model.autoMask = 1;
    cfg.model.intensity = 1.0f;
    std::mutex cfgMu;
    std::atomic<bool> quit{false};
    Stats stats;
    std::thread stdinThread(StdinLoop, &cfg, &cfgMu, &quit, &stats);

    GpuContext gpu;
    NgxSession ngx;
    Capture cap;
    Overlay overlay;
    LivePasses passes;
    NvofFlow nvof;
    Fwd fwd;
    NVSDK_NGX_Parameter* nrParams = nullptr;
    void* feature = nullptr;
    int rc = 0;
    try {
        gpu.Initialize(false, args.adapter);
        const unsigned drv = NvidiaDriverNumber(gpu.UmdDriverVersion());
        LogInfo("gpu: %s, driver %s", gpu.AdapterName().c_str(), NvidiaDriverVersionString(gpu.UmdDriverVersion()).c_str());
        if (gpu.VendorId() != 0x10DE) throw ToolError("not an NVIDIA GPU");
        if (drv && drv < kMinNvidiaDriverForNr) throw ToolError("driver too old for Neural Rendering");
        SetupArchSpoof();
        ngx.Init(gpu.Device(), DefaultDllSearchPaths(args.dllDir), args.verbose);
        if (NVSDK_NGX_FAILED(NVSDK_NGX_D3D12_AllocateParameters(&nrParams)) || !nrParams)
            throw ToolError("NVSDK_NGX_D3D12_AllocateParameters failed");
        if (!fwd.Load()) throw ToolError("forwarder nvngx.dll_dlssnr.dll not found next to the exe");
        std::string err;
        if (!cap.Init(gpu.Adapter(), gpu.Device(), &err)) throw ToolError(err);
        passes.Init(gpu.Device());
        LogInfo("ready (snippet %s)", Widen2Narrow(SnippetPath(args.dllDir)).c_str());
        Reply("{\"ok\":1,\"route\":\"gpu\",\"gpu\":\"" + JEscape(gpu.AdapterName()) + "\"}");
    } catch (const std::exception& e) {
        LogErr("init: %s", e.what());
        Reply(std::string("{\"ok\":0,\"err\":\"") + JEscape(e.what()) + "\"}");
        stats.running = false;
        quit = true;
        stdinThread.detach();
        return 1;
    }

    const std::wstring snippet = SnippetPath(args.dllDir);
    const std::wstring data = DataPath();

    GpuTexture origLow, color, depth, motion, out, finalLow, thumb, presentTex;
    int nrW = 0, nrH = 0, capW = 0, capH = 0, scale = 0;
    unsigned featureGen = ~0u;
    bool needReset = true;
    bool haveFinal = false;
    unsigned presentedGen = ~0u;
    std::vector<float> prevThumb;
    std::vector<double> stamps;
    auto lastStat = std::chrono::steady_clock::now();
    HMONITOR curMon = nullptr;
    HWND capHwnd = nullptr;          // window the capture (WGC or DDA) is bound to
    bool capWgcPref = true;
    bool useDda = false;             // fallback when WGC is unavailable or not wanted

    auto releaseFeature = [&]() {
        if (feature) {
            fwd.release(feature);
            feature = nullptr;
        }
    };

    while (!quit.load()) {
        if (!args.headless) overlay.Pump();
        Cfg c;
        {
            std::lock_guard<std::mutex> g(cfgMu);
            c = cfg;
        }
        try {
            if (!c.hwnd || !IsWindow(c.hwnd) || IsIconic(c.hwnd) || c.view == 2) {
                if (!args.headless) overlay.Hide();
                stats.visible = false;
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                continue;
            }
            // Screen rect of the viewport region.
            RECT client{};
            GetClientRect(c.hwnd, &client);
            int l = std::max(0, c.rect[0]), t = std::max(0, c.rect[1]);
            int w = std::min(c.rect[2], static_cast<int>(client.right) - l);
            int h = std::min(c.rect[3], static_cast<int>(client.bottom) - t);
            if (w < 32 || h < 32) {
                if (!args.headless) overlay.Hide();
                stats.visible = false;
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                continue;
            }
            w &= ~1;
            h &= ~1;
            if (!args.headless) stats.visible = overlay.Visible();   // Windows re-shows it with a restored owner
            POINT org{l, t};
            ClientToScreen(c.hwnd, &org);
            RECT screen{org.x, org.y, org.x + w, org.y + h};
            std::string err;
            // The overlay window is owned by the Blender window (created once we know it).
            if (!args.headless && overlay.Owner() != c.hwnd) {
                if (!overlay.Create(c.hwnd, &err)) throw ToolError(err);
            }
            // Capture backend, bound to the Blender window: per-window capture first (recorders can
            // see the overlay), desktop duplication + self-exclusion as the fallback.
            if (capHwnd != c.hwnd || capWgcPref != c.preferWgc) {
                capHwnd = c.hwnd;
                capWgcPref = c.preferWgc;
                useDda = !c.preferWgc || !cap.StartWgc(c.hwnd, &err);
                if (useDda) {
                    cap.StopWgc();
                    if (c.preferWgc) LogWarn("%s -> falling back to desktop duplication", err.c_str());
                    LogInfo("capture: desktop duplication (overlay excluded from screen recorders)");
                    if (!args.headless) overlay.SetExcludeFromCapture(true);
                    curMon = nullptr;
                } else {
                    LogInfo("capture: Windows.Graphics.Capture of the Blender window");
                    if (!args.headless) overlay.SetExcludeFromCapture(false);
                }
            }
            HMONITOR mon = MonitorFromWindow(c.hwnd, MONITOR_DEFAULTTONEAREST);
            if (useDda && (mon != curMon || cap.Monitor() != mon)) {
                if (!cap.SelectOutput(mon, &err)) throw ToolError(err);
                curMon = mon;
            }
            const int wantScale = c.half ? 2 : 1;
            const int wantNrW = w / wantScale, wantNrH = h / wantScale;
            const bool sizeChanged = (w != capW || h != capH || wantScale != scale || wantNrW != nrW || wantNrH != nrH);
            if (sizeChanged) {
                capW = w;
                capH = h;
                scale = wantScale;
                nrW = wantNrW;
                nrH = wantNrH;
                origLow = gpu.CreateTexture(nrW, nrH, DXGI_FORMAT_R16G16B16A16_FLOAT, true, L"liveOrig");
                color = gpu.CreateTexture(nrW, nrH, DXGI_FORMAT_R16G16B16A16_FLOAT, true, L"liveColor");
                depth = gpu.CreateTexture(nrW, nrH, DXGI_FORMAT_R32_FLOAT, true, L"liveDepth");
                motion = gpu.CreateTexture(nrW, nrH, DXGI_FORMAT_R16G16_FLOAT, true, L"liveMotion");
                out = gpu.CreateTexture(nrW, nrH, DXGI_FORMAT_R16G16B16A16_FLOAT, true, L"liveOut");
                finalLow = gpu.CreateTexture(nrW, nrH, DXGI_FORMAT_R8G8B8A8_UNORM, true, L"liveFinal");
                thumb = gpu.CreateTexture(32, 32, DXGI_FORMAT_R32_FLOAT, true, L"liveThumb");
                presentTex = gpu.CreateTexture(w, h, DXGI_FORMAT_R8G8B8A8_UNORM, true, L"livePresent");
                std::vector<float> zeros(static_cast<size_t>(nrW) * nrH, 0.0f);
                gpu.UploadR32Float(depth, zeros);
                gpu.Begin();
                gpu.Transition(origLow, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(color, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(depth, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(motion, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(out, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(finalLow, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(thumb, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(presentTex, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.EndAndWait();
                releaseFeature();
                nvof.Shutdown();
                if (!nvof.Init(gpu, nrW, nrH, nrW, nrH)) LogWarn("NVOF unavailable: no motion vectors");
                prevThumb.clear();
                needReset = true;
                haveFinal = false;
            }
            if (!args.headless && (sizeChanged || !overlay.HasSwapChain())) {
                // (re)created together with the window: the DComp target is per HWND
                ComPtr<IDXGIFactory6> factory;
                CHECK_HR(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory)));
                if (!overlay.EnsureSwapChain(factory.Get(), gpu.Queue(), w, h, &err)) throw ToolError(err);
            }
            if (!feature || featureGen != c.modelGen) {
                releaseFeature();
                ID3D12GraphicsCommandList* cl = gpu.Begin();
                feature = fwd.create(snippet.c_str(), data.c_str(), gpu.Device(), cl, nrParams,
                                     static_cast<unsigned>(nrW), static_cast<unsigned>(nrH),
                                     static_cast<unsigned>(nrW), static_cast<unsigned>(nrH), &c.model);
                gpu.EndAndWait();
                if (!feature) throw ToolError(fwd.Crashed() ? "CreateFeature 18 faulted inside the snippet"
                                                             : "CreateFeature 18 failed");
                featureGen = c.modelGen;
                needReset = true;
                LogInfo("NR feature %dx%d (style %u tone %.1f structure %.1f skin %.1f)", nrW, nrH,
                        c.model.style, c.model.localTone, c.model.localStructure, c.model.skinStructure);
            }

            // ---- capture ----
            const auto tCap0 = std::chrono::steady_clock::now();
            int got = useDda ? cap.Acquire(screen, c.bench || needReset, &err)
                             : cap.AcquireWgc(c.hwnd, l, t, w, h, c.bench || needReset, &err);
            if (got < 0) {
                if (!err.empty()) LogWarn("capture: %s (restarting capture)", err.c_str());
                curMon = nullptr;
                capHwnd = nullptr;
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                continue;
            }
            stats.capMs = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - tCap0).count();
            const bool paramsChanged = (c.gen != presentedGen);
            if (got == 0 && !paramsChanged) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
                continue;
            }
            const auto tFrame0 = std::chrono::steady_clock::now();
            bool ranNr = false;
            if (got == 1) {
                // GPU waits for the D3D11 copy.
                gpu.Queue()->Wait(cap.Fence12(), cap.FenceValue());
                ID3D12Resource* shared = cap.Shared12();
                // decode + thumbnail
                passes.BeginFrame();
                ID3D12GraphicsCommandList* cl = gpu.Begin();
                Barrier(cl, shared, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                LiveConsts k{};
                k.out[0] = static_cast<uint32_t>(nrW);
                k.out[1] = static_cast<uint32_t>(nrH);
                k.scale = static_cast<uint32_t>(scale);
                k.mode = 0;
                passes.Record(cl, shared, DXGI_FORMAT_B8G8R8A8_UNORM, shared, DXGI_FORMAT_B8G8R8A8_UNORM,
                              origLow.res.Get(), origLow.fmt, k);
                LiveConsts kt{};
                kt.out[0] = 32;
                kt.out[1] = 32;
                kt.mode = 2;
                passes.Record(cl, shared, DXGI_FORMAT_B8G8R8A8_UNORM, shared, DXGI_FORMAT_B8G8R8A8_UNORM,
                              thumb.res.Get(), thumb.fmt, kt);
                gpu.UavBarrier(origLow);
                gpu.Transition(origLow, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                gpu.Transition(color, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.RecordEncodeSrgb(origLow, color);
                Barrier(cl, shared, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
                gpu.EndAndWait();
                std::vector<float> th = gpu.ReadbackR32Float(thumb);
                bool same = (!prevThumb.empty() && th == prevThumb);
                prevThumb = th;
                if (same && !paramsChanged && !c.bench) {
                    // Our own overlay (or an unrelated change) triggered the frame; picture unchanged.
                    gpu.Begin();
                    gpu.Transition(origLow, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                    gpu.EndAndWait();
                    continue;
                }
                // motion vectors (NVOF runs its own lists)
                if (nvof.Ok()) nvof.Compute(gpu, origLow, motion, needReset);
                // evaluate
                cl = gpu.Begin();
                gpu.Transition(color, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                gpu.Transition(depth, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                gpu.Transition(motion, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                gpu.Transition(out, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.RecordTimestampBegin();
                const int er = fwd.evaluate(cl, feature, nrParams, color.res.Get(), depth.res.Get(),
                                            motion.res.Get(), out.res.Get(), static_cast<unsigned>(nrW),
                                            static_cast<unsigned>(nrH), static_cast<unsigned>(nrW),
                                            static_cast<unsigned>(nrH), needReset ? 1 : 0);
                gpu.RecordTimestampEnd();
                gpu.EndAndWait();
                if (NVSDK_NGX_FAILED(static_cast<NVSDK_NGX_Result>(er))) {
                    throw ToolError(fwd.Crashed() ? "evaluate faulted inside the snippet" : "evaluate failed");
                }
                stats.nrMs = gpu.LastGpuMs();
                // composite (linear original + model output -> 8-bit sRGB at NR res)
                gpu.Begin();
                gpu.Transition(out, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                gpu.Transition(color, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(motion, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(finalLow, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.RecordComposite(origLow, out, finalLow, c.detail, c.colour);
                gpu.Transition(origLow, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.EndAndWait();
                needReset = false;
                haveFinal = true;
                ranNr = true;
            }
            if (!haveFinal) continue;
            // ---- present ----
            if (!args.headless) {
                gpu.Queue()->Wait(cap.Fence12(), cap.FenceValue());
                passes.BeginFrame();
                ID3D12GraphicsCommandList* cl = gpu.Begin();
                ID3D12Resource* bb = overlay.CurrentBackBuffer();
                ID3D12Resource* shared = cap.Shared12();
                Barrier(cl, shared, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                gpu.Transition(presentTex, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.Transition(finalLow, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                LiveConsts k{};
                k.out[0] = static_cast<uint32_t>(w);
                k.out[1] = static_cast<uint32_t>(h);
                k.scale = static_cast<uint32_t>(scale);
                k.mode = 1;
                k.splitX = static_cast<uint32_t>(std::lround(std::clamp(c.split, 0.0f, 1.0f) * w));
                k.view = static_cast<uint32_t>(c.view);
                k.gain = (c.colour < 0.5f) ? 1u : 0u;
                k.numHoles = static_cast<uint32_t>(std::min<size_t>(6, c.holes.size()));
                for (uint32_t i = 0; i < k.numHoles; ++i) {
                    k.holes[i][0] = c.holes[i].x;
                    k.holes[i][1] = c.holes[i].y;
                    k.holes[i][2] = c.holes[i].w;
                    k.holes[i][3] = c.holes[i].h;
                }
                passes.Record(cl, shared, DXGI_FORMAT_B8G8R8A8_UNORM, finalLow.res.Get(), finalLow.fmt,
                              presentTex.res.Get(), presentTex.fmt, k);
                // composition swap chains refuse UAV usage: write our texture, then copy it in.
                gpu.Transition(presentTex, D3D12_RESOURCE_STATE_COPY_SOURCE);
                Barrier(cl, bb, D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_COPY_DEST);
                cl->CopyResource(bb, presentTex.res.Get());
                Barrier(cl, bb, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PRESENT);
                Barrier(cl, shared, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
                gpu.Transition(finalLow, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
                gpu.EndAndWait();
                if (!c.dump.empty()) {
                    // debug: the frame exactly as presented (premultiplied RGBA), alpha kept.
                    std::vector<uint8_t> px = gpu.ReadbackRgba8(presentTex);
                    stbi_write_png(c.dump.c_str(), w, h, 4, px.data(), w * 4);
                    LogInfo("dumped %s", c.dump.c_str());
                    std::lock_guard<std::mutex> g(cfgMu);
                    cfg.dump.clear();
                }
                overlay.Present();
                overlay.Place(screen.left, screen.top, w, h, true);
                stats.visible = true;
            }
            presentedGen = c.gen;
            if (ranNr) {
                const auto now = std::chrono::steady_clock::now();
                stats.ms = std::chrono::duration<double, std::milli>(now - tFrame0).count();
                ++stats.frames;
                const double ts = std::chrono::duration<double>(now.time_since_epoch()).count();
                stamps.push_back(ts);
                while (!stamps.empty() && ts - stamps.front() > 2.0) stamps.erase(stamps.begin());
                if (stamps.size() > 1) stats.fps = (stamps.size() - 1) / (stamps.back() - stamps.front());
                if (args.verbose && std::chrono::duration<double>(now - lastStat).count() > 2.0) {
                    lastStat = now;
                    LogInfo("live %.1f fps, nr %.1f ms, frame %.1f ms", stats.fps.load(), stats.nrMs.load(), stats.ms.load());
                }
                stats.SetErr("");
            }
        } catch (const winrt::hresult_error& e) {
            const std::string msg = "WinRT: " + Widen2Narrow(std::wstring(e.message()));
            LogErr("%s", msg.c_str());
            stats.SetErr(msg);
            capHwnd = nullptr;
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        } catch (const std::exception& e) {
            LogErr("%s", e.what());
            stats.SetErr(e.what());
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
            if (fwd.Crashed()) {
                rc = 2;
                break;
            }
        }
    }

    stats.running = false;
    quit = true;
    cap.StopWgc();
    if (!args.headless) overlay.Destroy();
    releaseFeature();
    nvof.Shutdown();
    if (nrParams) NVSDK_NGX_D3D12_DestroyParameters(nrParams);
    ngx.Shutdown();
    if (stdinThread.joinable()) {
        // stdin may still be blocked in getline; the parent closes the pipe on exit.
        stdinThread.detach();
    }
    LogInfo("live: %lld frames", stats.frames.load());
    return rc;
}
