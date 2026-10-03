# -*- coding: utf-8 -*-
"""扫描打包目录里所有 pyd/dll 的导入表，找出包内缺失的依赖 DLL。"""
import os
import sys

try:
    import pefile
except ImportError:
    print("pefile not available")
    sys.exit(1)

pkg = sys.argv[1] if len(sys.argv) > 1 else r"dist-out\微博收藏下载器\_internal"

have = set()
for root, dirs, files in os.walk(pkg):
    for f in files:
        have.add(f.lower())

SYS_PREFIXES = (
    "api-ms-win", "ext-ms-win", "kernel32", "kernelbase", "user32", "gdi32", "advapi32",
    "shell32", "ole32", "oleaut32", "comdlg32", "comctl32", "ntdll", "msvcrt", "sechost",
    "rpcrt4", "ws2_32", "wsock32", "crypt32", "bcrypt", "bcryptprimitives", "ncrypt",
    "dwmapi", "uxtheme", "imm32", "version", "winspool", "winmm", "netapi32", "userenv",
    "psapi", "shlwapi", "mpr", "dnsapi", "iphlpapi", "secur32", "wintrust", "powrprof",
    "setupapi", "cfgmgr32", "mswsock", "normaliz", "dbghelp", "opengl32", "glu32",
    "msimg32", "usp10", "gdiplus", "winhttp", "urlmon", "wininet", "twinapi", "cryptbase",
    "profapi", "wtsapi32", "authz", "slc", "d3d11", "dxgi", "d2d1", "dwrite", "windowscodecs",
)

missing = {}
for root, dirs, files in os.walk(pkg):
    for f in files:
        low = f.lower()
        if not low.endswith((".pyd", ".dll")):
            continue
        path = os.path.join(root, f)
        try:
            pe = pefile.PE(path, fast_load=True)
            pe.parse_data_directories(
                directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
            deps = [e.dll.decode(errors="ignore").lower()
                    for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
            pe.close()
        except Exception:
            continue
        for d in deps:
            if d in have or d.endswith(".pyd"):
                continue
            if d.startswith(SYS_PREFIXES):
                continue
            missing.setdefault(d, []).append(f)

print("=== DLLs referenced but NOT present in package ===")
if not missing:
    print("  (none)")
for d in sorted(missing):
    users = ", ".join(sorted(set(missing[d]))[:3])
    print("  %-32s <- %s" % (d, users))
