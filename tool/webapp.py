"""
webapp.py -- PCB 成品预演工具的本地网页版

开发模式:
    python tool/webapp.py            # http://127.0.0.1:8777
    python tool/webapp.py --port 9000

打包后（PyInstaller onefile）由 launcher.py 调用：静态资源在临时解包目录，
产出写到 %LOCALAPPDATA%\\PCBCamPreview，避免装在只读目录时无法写文件。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FROZEN = bool(getattr(sys, "frozen", False))
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))

if FROZEN:
    WEB_DIR = os.path.join(getattr(sys, "_MEIPASS", TOOL_DIR), "web")
    WORK_ROOT = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PCBCamPreview"
    )
else:
    WEB_DIR = os.path.join(TOOL_DIR, "web")
    WORK_ROOT = os.path.join(os.path.dirname(TOOL_DIR), "out")

OUT_DIR = os.path.join(WORK_ROOT, "web")
UP_DIR = os.path.join(WORK_ROOT, "_uploads")

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


# ---------------------------------------------------------------- 任务执行


class _StreamTap:
    """把 cam_preview 的 print 输出接进任务日志。"""

    def __init__(self, job):
        self.job = job

    def write(self, s):
        for line in s.splitlines():
            if not line.strip():
                continue
            self.job["log"] = (self.job.get("log") or [])[-79:] + [line.rstrip()]
            m = re.search(r"\[(\w+)\]\s+(\w+)\s", line)
            if m:
                self.job["step"] = f"{m.group(1)} · {m.group(2)}"

    def flush(self):
        pass


def run_job(jid: str, args: list[str], out_dir: str):
    job = JOBS[jid]
    job["status"] = "running"
    try:
        if FROZEN:
            # 打包后没有独立的 python 解释器，改为进程内调用
            import cam_preview

            with contextlib.redirect_stdout(_StreamTap(job)), \
                    contextlib.redirect_stderr(_StreamTap(job)):
                cam_preview.main(args)
            rc = 0
        else:
            p = subprocess.Popen(
                [sys.executable, os.path.join(TOOL_DIR, "cam_preview.py")] + args,
                cwd=os.path.dirname(TOOL_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            tap = _StreamTap(job)
            for line in p.stdout:  # type: ignore
                if line.strip():
                    tap.write(line)
            p.wait()
            rc = p.returncode

        if rc != 0:
            job["status"] = "error"
            job["error"] = "分析失败，请查看日志"
            return

        sp = os.path.join(out_dir, "stats.json")
        if os.path.exists(sp):
            job["stats"] = json.load(open(sp, encoding="utf-8"))
        job["status"] = "done"
    except Exception as e:  # pragma: no cover
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "pcbcam/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _file(self, path):
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    def _safe(self, base, rel):
        p = os.path.normpath(os.path.join(base, rel))
        return p if p.startswith(os.path.normpath(base)) else None

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            return self._file(os.path.join(WEB_DIR, "index.html"))
        if path.startswith("/static/"):
            p = self._safe(WEB_DIR, path[len("/static/"):])
            return self._file(p) if p else self._json({"error": "bad path"}, 400)

        if path == "/api/job":
            jid = (q.get("id") or [""])[0]
            job = JOBS.get(jid)
            if not job:
                return self._json({"error": "no such job"}, 404)
            return self._json(
                {
                    "status": job.get("status"),
                    "step": job.get("step", ""),
                    "log": job.get("log", []),
                    "stats": job.get("stats"),
                    "error": job.get("error"),
                }
            )

        if path.startswith("/r/"):
            parts = path[len("/r/"):].split("/", 1)
            if len(parts) != 2:
                return self._json({"error": "bad path"}, 400)
            jid, name = parts
            if jid not in JOBS:
                return self._json({"error": "no such job"}, 404)
            p = self._safe(os.path.join(OUT_DIR, jid), name)
            return self._file(p) if p else self._json({"error": "bad path"}, 400)

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)

        if u.path == "/api/upload":
            slot = (q.get("slot") or ["eda"])[0]
            data = self.rfile.read(n) if n else b""
            if len(data) < 100:
                return self._json({"error": "文件为空"}, 400)
            if len(data) > 120 * 1024 * 1024:
                return self._json({"error": "文件超过 120MB"}, 400)
            os.makedirs(UP_DIR, exist_ok=True)
            token = uuid.uuid4().hex[:12]
            ext = ".zip" if data[:2] == b"PK" else ".bin"
            fp = os.path.join(UP_DIR, f"{slot}_{token}{ext}")
            with open(fp, "wb") as f:
                f.write(data)
            return self._json({"token": fp, "size": len(data)})

        if u.path == "/api/analyze":
            raw = self.rfile.read(n) if n else b"{}"
            try:
                cfg = json.loads(raw.decode("utf-8"))
            except Exception:
                return self._json({"error": "bad json"}, 400)

            eda = cfg.get("eda")
            if not eda or not os.path.exists(eda):
                return self._json({"error": "缺少 EDA 文件"}, 400)

            jid = time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
            out_dir = os.path.join(OUT_DIR, jid)
            os.makedirs(out_dir, exist_ok=True)

            args = [
                "--eda", eda, "--out", out_dir,
                "--mask", cfg.get("mask", "white"),
                "--silk", cfg.get("silk", "black"),
                "--finish", cfg.get("finish", "enig"),
                "--width", str(int(cfg.get("width", 2000))),
                "--silk-clearance", str(float(cfg.get("silkClearance", 0))),
                "--mask-expand", str(float(cfg.get("maskExpand", 0))),
                "--cutout-min", str(float(cfg.get("cutoutMin", 1.0))),
            ]
            if cfg.get("jlc") and os.path.exists(cfg["jlc"]):
                args += ["--jlc", cfg["jlc"]]
            if not cfg.get("annotate", True):
                args += ["--no-annotate"]

            with LOCK:
                JOBS[jid] = {"status": "queued", "log": [], "step": "排队中"}
            threading.Thread(
                target=run_job, args=(jid, args, out_dir), daemon=True
            ).start()
            return self._json({"job": jid})

        return self._json({"error": "not found"}, 404)


def serve(host="127.0.0.1", port=8777):
    os.makedirs(OUT_DIR, exist_ok=True)
    return ThreadingHTTPServer((host, port), Handler)


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = serve(a.host, a.port)
    print(f"PCB 成品预演 · 网页版已启动 ->  http://{a.host}:{a.port}")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
