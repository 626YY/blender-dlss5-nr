"""One-off helper used when the repo was first published: replace machine-specific paths with env vars
and assert nothing private is left. Safe to re-run; kept for reference."""
import io
import os
import re
import sys

R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def sub(path, pairs, must_change=True):
    p = os.path.join(R, path)
    s = io.open(p, encoding="utf-8").read()
    o = s
    for a, b in pairs:
        s = s.replace(a, b)
    if must_change and s == o:
        print("WARN unchanged:", path)
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)


ENV_ROOT = 'os.environ.get("DLSS5_NR_ROOT", r"C:\\ComfyUI\\custom_nodes\\ComfyUI-DLSS5-NR")'
ENV_BPY = 'os.environ.get("BLENDER_PYTHON", r"C:\\Program Files\\Blender Foundation\\Blender 5.1\\5.1\\python\\bin\\python.exe")'

sub("addon/dlss5_nr/__init__.py", [
    ('DEFAULT_NR_ROOT = r"H:\\ComfyUI-aki-v1.4\\custom_nodes\\ComfyUI-DLSS5-NR"',
     'DEFAULT_NR_ROOT = ""   # ComfyUI-DLSS5-NR 的安装目录(含 native/bin 与 runtime),在「高级」里填'),
    (',\n                 r"H:\\ZLHTD\\src\\dlss5_live\\out\\dlss5_live.exe"]', ']'),
    ('留空则找扩展目录 bin/ 或 H:\\\\ZLHTD\\\\src\\\\dlss5_live\\\\out', '留空则找扩展目录 bin/'),
], must_change=False)

for f in ("nr_sweep.py", "nr_sweep2.py", "style_sweep.py", "test_worker.py", "test_worker2.py", "test_monitor.py"):
    sub("scripts/" + f, [
        ('ROOT = r"H:\\ComfyUI-aki-v1.4\\custom_nodes\\ComfyUI-DLSS5-NR"', "ROOT = " + ENV_ROOT),
        ('BPY = r"H:\\blender-5.1.2-windows-x64\\5.1\\python\\bin\\python.exe"', "BPY = " + ENV_BPY),
    ], must_change=False)
for f in ("test_live.py", "test_zorder.py"):
    sub("scripts/" + f, [
    ('EXE = r"H:\\ZLHTD\\src\\dlss5_live\\out\\dlss5_live.exe"',
     'EXE = os.environ.get("DLSS5_LIVE_EXE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "native", "dlss5_live", "out", "dlss5_live.exe"))'),
    ('DLLDIR = r"H:\\dpsk\\dlss5-eevee\\video2dlssnr\\out"',
     'DLLDIR = os.environ.get("DLSS5_DLL_DIR", r"C:\\ComfyUI\\custom_nodes\\ComfyUI-DLSS5-NR\\runtime")'),
], must_change=False)

for f in ("nr_sweep.py", "nr_sweep2.py", "style_sweep.py", "test_worker.py", "test_worker2.py", "test_monitor.py", "test_live.py"):
    p = os.path.join(R, "scripts", f)
    s = io.open(p, encoding="utf-8").read()
    if not re.search(r"^import .*\bos\b", s, re.M) and "\nimport os" not in s and not s.startswith("import os"):
        s = "import os\n" + s
        io.open(p, "w", encoding="utf-8", newline="\n").write(s)

bad = []
for dp, dn, fn in os.walk(R):
    if ".git" in dp or "video2dlssnr" in dp:
        continue
    for f in fn:
        if f == "_sanitize_repo.py":
            continue
        p = os.path.join(dp, f)
        s = io.open(p, encoding="utf-8", errors="ignore").read()
        for needle in ("H:\\", "H:/", "art14", "ComfyUI-aki", "ZLHTD", "dpsk", "Violet", "Capheny", "AOV_"):
            if needle in s:
                bad.append((os.path.relpath(p, R), needle))
print("scan:", bad if bad else "clean")
sys.exit(1 if bad else 0)
