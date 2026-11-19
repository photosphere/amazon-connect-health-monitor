#!/usr/bin/env python3
"""
serve_local.py — 本地预览前端(不连接 AWS)

    python3 dev/serve_local.py [--port 8765]
    浏览器打开 http://localhost:8765/

- 静态文件来自 lib/web/
- /aws-config.js 返回 {mock: true}, auth.js 因此跳过 Cognito 登录
- index.html 在 auth.js 前注入 dev/mock-api.js(内存版 API, 配置存 localStorage)
"""
import argparse
import http.server
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lib", "web")
DEV = os.path.join(ROOT, "dev")
MOCK_CONFIG = b'window.__AWS_CONFIG__ = { mock: true, region: "us-east-1", apiUrl: "" };\n'
# 与 setup_monitor.build_csp() 相同的策略(本地 mock 不访问外部地址, connect-src 只允许 self),
# 用来在本地提前发现违反 CSP 的写法(内联脚本、外部资源等)
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'; object-src 'none'")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/aws-config.js":
            return self._send(MOCK_CONFIG, "application/javascript; charset=utf-8")
        if path == "/dev/mock-api.js":
            with open(os.path.join(DEV, "mock-api.js"), "rb") as f:
                return self._send(f.read(), "application/javascript; charset=utf-8")
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
                html = f.read()
            html = html.replace(
                '<script src="./auth.js"></script>',
                '<script src="/dev/mock-api.js"></script>\n<script src="./auth.js"></script>',
            )
            return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
        return super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    print(f"本地预览: http://localhost:{args.port}/  (mock 数据, Ctrl+C 退出)")
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
