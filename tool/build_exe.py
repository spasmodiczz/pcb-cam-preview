"""
build_exe.py -- 打包成 Windows 单文件 exe

用法:
    python tool/build_exe.py

产物: dist/PCBCamPreview.exe
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tool")
NAME = "PCBCamPreview"


def main() -> int:
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            print("缺少 PyInstaller，先执行: pip install -r requirements-dev.txt")
            return 1

    web_src = os.path.join(TOOL, "web")
    if not os.path.isfile(os.path.join(web_src, "index.html")):
        print(f"找不到前端资源: {web_src}")
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--name", NAME,
        "--add-data", f"{web_src}{os.pathsep}web",
        "--paths", TOOL,
        "--hidden-import", "shapely",
        "--hidden-import", "shapely.geometry",
        "--hidden-import", "shapely.ops",
        "--hidden-import", "shapely.affinity",
        "--console",
        os.path.join(TOOL, "launcher.py"),
    ]
    print("执行:", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        print("打包失败")
        return rc

    exe = os.path.join(ROOT, "dist", f"{NAME}.exe")
    if os.path.isfile(exe):
        mb = os.path.getsize(exe) / 1048576
        print(f"\n完成: {exe}  ({mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
