// live_main.cpp - entry point for dlss5_live.exe.
//
//   dlss5_live.exe [--dll-dir <dir with nvngx_dlssnr.dll>] [--adapter <i>] [--verbose] [--headless]
//
// Then JSON lines on stdin:
//   {"cmd":"start","cfg":{"hwnd":66832,"rect":[2,26,1353,1056],"holes":[[0,0,1353,26]],
//                         "split":0.5,"view":"SPLIT","half":true,"style":2,"tone":0,"structure":2,
//                         "skin":1.5,"automask":true,"mode":"COLOR","strength":1.0}}
//   {"cmd":"update","cfg":{...}}   {"cmd":"status"}   {"cmd":"stop"}
// Replies are single JSON lines on stdout. Logs go to stderr.
#include "common.h"

#include "live.h"

int main(int argc, char** argv) {
    LiveArgs a;
    for (int i = 1; i < argc; ++i) {
        const std::string s = argv[i];
        if (s == "--dll-dir" && i + 1 < argc) a.dllDir = argv[++i];
        else if (s == "--adapter" && i + 1 < argc) a.adapter = atoi(argv[++i]);
        else if (s == "--verbose" || s == "-v") a.verbose = true;
        else if (s == "--headless") a.headless = true;
        else if (s == "--help" || s == "-h") {
            std::printf("dlss5_live.exe [--dll-dir <dir>] [--adapter <i>] [--verbose] [--headless]\n");
            return 0;
        }
    }
    try {
        return RunLive(a);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "fatal: %s\n", e.what());
        return 1;
    }
}
