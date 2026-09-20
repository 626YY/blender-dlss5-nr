@echo off
rem Build dlss5_live.exe against the video2dlssnr sources (git submodule at ..\video2dlssnr).
rem Needs an MSVC x64 toolchain: Visual Studio 2022+/Build Tools, or a portable MSVC folder whose
rem setup_x64.bat you point MSVC_PORTABLE at (see docs/BUILD.md). Also needs
rem ..\video2dlssnr\third_party\nvngx\lib\nvsdk_ngx_d.lib from NVIDIA's DLSS SDK (not redistributed).
setlocal enabledelayedexpansion
set "ROOT=%~dp0"
set "V2D=%ROOT%..\video2dlssnr\"

set "VCVARS="
if defined MSVC_PORTABLE if exist "%MSVC_PORTABLE%\setup_x64.bat" set "VCVARS=%MSVC_PORTABLE%\setup_x64.bat"
if not defined VCVARS if exist "%ROOT%..\..\tools\msvc\setup_x64.bat" set "VCVARS=%ROOT%..\..\tools\msvc\setup_x64.bat"
set "PFX64=%ProgramFiles%"
set "PFX86=%ProgramFiles(x86)%"
for %%E in (2026 2025 2022) do (
    for %%D in (Community Professional Enterprise BuildTools Preview) do (
        if not defined VCVARS if exist "!PFX64!\Microsoft Visual Studio\%%E\%%D\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=!PFX64!\Microsoft Visual Studio\%%E\%%D\VC\Auxiliary\Build\vcvars64.bat"
        if not defined VCVARS if exist "!PFX86!\Microsoft Visual Studio\%%E\%%D\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=!PFX86!\Microsoft Visual Studio\%%E\%%D\VC\Auxiliary\Build\vcvars64.bat"
    )
)
if not defined VCVARS (
    echo ERROR: no MSVC x64 toolchain found. Install VS Build Tools, or set MSVC_PORTABLE to a portable MSVC folder.
    exit /b 1
)
call "!VCVARS!" >nul
where cl >nul 2>nul || (echo ERROR: cl not on PATH after "!VCVARS!" & exit /b 1)

if not exist "%V2D%src\nr.cpp" (
    echo ERROR: video2dlssnr submodule missing. Run: git submodule update --init
    exit /b 1
)

if not exist "%ROOT%build" mkdir "%ROOT%build"
if not exist "%ROOT%out" mkdir "%ROOT%out"

set "CFG=%~1"
if "%CFG%"=="" set "CFG=release"
if /i "%CFG%"=="debug" (
    set "OPT=/Od /Zi /MDd /D_DEBUG"
    set "NGXLIB=%V2D%third_party\nvngx\lib\nvsdk_ngx_d_dbg.lib"
) else (
    set "OPT=/O2 /MD /DNDEBUG"
    set "NGXLIB=%V2D%third_party\nvngx\lib\nvsdk_ngx_d.lib"
)
if not exist "!NGXLIB!" (
    echo ERROR: missing !NGXLIB!
    echo        Get it from https://github.com/NVIDIA/DLSS  ^(lib/Windows_x86_64/x64/^)
    exit /b 1
)
set "COMMON=/nologo /std:c++17 /EHsc /W3 !OPT! /D_CRT_SECURE_NO_WARNINGS /utf-8"
set "INCLUDES=/I "%V2D%third_party\nvngx\include" /I "%V2D%third_party\stb" /I "%V2D%third_party\nvof" /I "%V2D%src" /I "%V2D%forwarder" /I "%ROOT%.""
set "LIBS=d3d12.lib d3d11.lib dxgi.lib d3dcompiler.lib dxguid.lib dcomp.lib dwmapi.lib windowsapp.lib advapi32.lib user32.lib version.lib shell32.lib"
set "SHARED=%V2D%src\common.cpp %V2D%src\image.cpp %V2D%src\gpu.cpp %V2D%src\dlss.cpp %V2D%src\cli.cpp %V2D%src\pipeline.cpp %V2D%src\nr.cpp %V2D%src\archspoof.cpp %V2D%src\optflow.cpp %V2D%src\optflow_nvof.cpp %V2D%src\slprobe.cpp"

echo Building dlss5_live [%CFG%]...
cl !COMMON! !INCLUDES! /Fo"%ROOT%build\\" /Fe"%ROOT%out\dlss5_live.exe" !SHARED! "%ROOT%live.cpp" "%ROOT%live_main.cpp" /link "!NGXLIB!" !LIBS!
if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)

echo Building the forwarder shim nvngx.dll_dlssnr.dll...
if not exist "%ROOT%build\fwd" mkdir "%ROOT%build\fwd"
cl !COMMON! !INCLUDES! /Fo"%ROOT%build\fwd\\" /LD "%V2D%forwarder\nvngx_fwd.cpp" /Fe"%ROOT%out\nvngx.dll_dlssnr.dll" /link
if errorlevel 1 (
    echo BUILD FAILED ^(forwarder^)
    exit /b 1
)
echo OK: %ROOT%out\dlss5_live.exe  (copy out\dlss5_live.exe and out\nvngx.dll_dlssnr.dll into addon\dlss5_nr\bin\)
