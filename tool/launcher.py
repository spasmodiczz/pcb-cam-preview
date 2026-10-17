"""
launcher.py -- 打包入口

双击 exe 时：自动选一个空闲端口起本地服务，并用默认浏览器打开页面。
命令行带参数时（如 --port 9000）按 webapp.py 的规则处理。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser


def free_port(start: int = 8777) -> int:
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0  # 交给系统分配


def main():
    # --noconsole 打包时 sys.stdout 为 None，避免 print 抛错
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    import webapp

    argv = sys.argv[1:]
    if any(a.startswith("--") for a in argv):
        webapp.main()  # argparse 直接读 sys.argv
        return

    port = free_port()
    srv = webapp.serve("127.0.0.1", port)
    url = f"http://127.0.0.1:{port}"
    print("=" * 52)
    print("  PCB 成品预演 · 嘉立创 CAM 规则")
    print(f"  已在浏览器打开: {url}")
    print("  关闭本窗口即可退出")
    print("=" * 52)
    threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
