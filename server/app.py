"""HTTPS + WSS receiver server and local PC dashboard for iPhone Mic Bridge."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import ssl
import time
import webbrowser
from pathlib import Path
from typing import Optional

import qrcode
import qrcode.image.svg
from aiohttp import WSMsgType, web

from .audio_out import VirtualMicRouter, list_output_devices
from .certs import ensure_certificates, get_local_ips

ROOT_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT_DIR / "docs"
CERTS_DIR = ROOT_DIR / "certs"
DEFAULT_PAGES_URL = "https://alexcrdaf.github.io/iphone-mic/"


def _cors_headers(origin: Optional[str]) -> dict[str, str]:
    allow_origin = origin if origin else "*"
    return {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Private-Network": "true",
    }


class MicBridgeServer:
    def __init__(
        self,
        port: int = 8443,
        dashboard_port: int = 8780,
        pages_url: str = DEFAULT_PAGES_URL,
        device_index: Optional[int] = None,
    ) -> None:
        self.port = port
        self.dashboard_port = dashboard_port
        self.pages_url = pages_url.rstrip("/") + "/"
        self.router = VirtualMicRouter(device_index=device_index)
        self.local_ips = get_local_ips() or ["127.0.0.1"]
        self.primary_ip = self.local_ips[0]

        self.active_ws: Optional[web.WebSocketResponse] = None
        self.client_info: dict = {
            "connected": False,
            "remoteIp": None,
            "mode": None,
            "sampleRate": 48000,
            "micLabel": None,
            "packets": 0,
            "bytes": 0,
            "connectedAt": None,
        }
        self._last_rx_ts = 0.0
        self.ca_cert_path: Optional[Path] = None

    def start_audio(self) -> None:
        self.router.start(input_samplerate=48000)

    def stop_audio(self) -> None:
        self.router.stop()

    def status_payload(self) -> dict:
        host = f"{self.primary_ip}:{self.port}"
        return {
            "ok": True,
            "port": self.port,
            "primaryIp": self.primary_ip,
            "allIps": self.local_ips,
            "host": host,
            "localUrl": f"https://{host}/",
            "pagesUrl": f"{self.pages_url}?host={host}",
            "caUrl": f"https://{host}/ca.crt",
            "trustUrl": f"https://{host}/trust",
            "audio": self.router.stats(),
            "client": self.client_info,
            "devices": [
                {
                    "index": d.index,
                    "name": d.name,
                    "hostApi": d.hostapi_name,
                    "defaultSampleRate": d.default_samplerate,
                    "isVbCable": d.is_vbcable,
                }
                for d in list_output_devices()
            ],
        }

    # --- HTTPS / WSS handlers (for iPhone Safari & GitHub Pages) ---

    async def handle_options(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=_cors_headers(request.headers.get("Origin")))

    async def handle_health(self, request: web.Request) -> web.Response:
        payload = {
            "ok": True,
            "app": "iphone-mic-pc",
            "host": f"{self.primary_ip}:{self.port}",
            "vbcableReady": bool(self.router.device_info and self.router.device_info.is_vbcable),
            "audioActive": bool(self.router.stats()["active"]),
            "connected": self.client_info["connected"],
        }
        return web.json_response(payload, headers=_cors_headers(request.headers.get("Origin")))

    async def handle_trust(self, request: web.Request) -> web.Response:
        pages_back = f"{self.pages_url}?host={self.primary_ip}:{self.port}&trusted=1"
        html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Сертификат ПК подтверждён</title>
  <style>
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      background: #0b1020; color: #eef2ff; display: grid; place-items: center; min-height: 100vh; padding: 24px;
    }}
    .card {{
      max-width: 440px; width: 100%; background: #151c33; border: 1px solid #2a355c;
      border-radius: 18px; padding: 24px; text-align: center;
    }}
    h1 {{ font-size: 20px; margin: 0 0 10px; }}
    p {{ color: #a9b5d9; line-height: 1.5; font-size: 15px; }}
    .btn {{
      display: block; margin-top: 14px; padding: 14px 18px; border-radius: 12px;
      text-decoration: none; font-weight: 600; font-size: 15px;
    }}
    .primary {{ background: #3b82f6; color: #fff; }}
    .secondary {{ background: #222d4f; color: #dbeafe; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Соединение с ПК доверено</h1>
    <p>Safari теперь разрешает передачу звука с микрофона на ваш ноутбук ({self.primary_ip}:{self.port}).</p>
    <a class="btn primary" href="{pages_back}">Вернуться на сайт GitHub Pages</a>
    <a class="btn secondary" href="/">Открыть интерфейс прямо здесь</a>
  </div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html", headers=_cors_headers(request.headers.get("Origin")))

    async def handle_ca_cert(self, request: web.Request) -> web.Response:
        if not self.ca_cert_path or not self.ca_cert_path.exists():
            raise web.HTTPNotFound()
        return web.Response(
            body=self.ca_cert_path.read_bytes(),
            content_type="application/x-x509-ca-cert",
            headers={
                "Content-Disposition": 'attachment; filename="iPhoneMicLocalCA.crt"',
                **_cors_headers(request.headers.get("Origin")),
            },
        )

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=1024 * 1024, heartbeat=15.0)
        await ws.prepare(request)

        if self.active_ws is not None and not self.active_ws.closed:
            await self.active_ws.close(code=4000, message=b"Replaced by new connection")

        self.active_ws = ws
        self.client_info = {
            "connected": True,
            "remoteIp": request.remote,
            "mode": "standard",
            "sampleRate": 48000,
            "micLabel": "iPhone Microphone",
            "packets": 0,
            "bytes": 0,
            "connectedAt": time.time(),
        }
        self._last_rx_ts = time.time()

        await ws.send_json({"type": "server_status", "audio": self.router.stats()})

        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self.router.push_pcm_int16(msg.data)
                    self.client_info["packets"] += 1
                    self.client_info["bytes"] += len(msg.data)
                    self._last_rx_ts = time.time()
                elif msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    msg_type = data.get("type")
                    if msg_type == "hello":
                        sr = int(data.get("sampleRate", 48000))
                        self.client_info["sampleRate"] = sr
                        self.client_info["mode"] = str(data.get("mode", "voice"))
                        self.client_info["micLabel"] = str(data.get("micLabel") or "iPhone Microphone")
                        self.router.set_input_samplerate(sr)
                        await ws.send_json({"type": "server_status", "audio": self.router.stats()})
                    elif msg_type == "config":
                        self._apply_control_payload(data)
                        if "mode" in data:
                            self.client_info["mode"] = str(data["mode"])
                        await ws.send_json({"type": "server_status", "audio": self.router.stats()})
                    elif msg_type == "ping":
                        await ws.send_json({"type": "pong", "t": data.get("t"), "audio": self.router.stats()})
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if self.active_ws is ws:
                self.active_ws = None
                self.client_info["connected"] = False
                self.router.rms_level = 0.0

        return ws

    # --- Local PC Dashboard handlers (http://127.0.0.1:8780) ---

    async def handle_pc_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(DOCS_DIR / "pc.html")

    async def handle_api_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.status_payload())

    async def handle_api_device(self, request: web.Request) -> web.Response:
        data = await request.json()
        idx = int(data["deviceIndex"])
        self.router.set_device(idx)
        return web.json_response(self.status_payload())

    def _apply_control_payload(self, data: dict) -> None:
        if "muted" in data:
            self.router.muted = bool(data["muted"])
        self.router.dsp.set_config(
            gain=float(data["gain"]) if "gain" in data else None,
            noise_mode=str(data["noiseMode"]) if "noiseMode" in data else None,
            hpf_enabled=bool(data["hpfEnabled"]) if "hpfEnabled" in data else None,
            agc_enabled=bool(data["agcEnabled"]) if "agcEnabled" in data else None,
        )
        if data.get("calibrateNoise"):
            self.router.dsp.start_noise_calibration(1.5)

    async def handle_api_control(self, request: web.Request) -> web.Response:
        data = await request.json()
        self._apply_control_payload(data)
        if self.active_ws is not None and not self.active_ws.closed:
            await self.active_ws.send_json({"type": "server_status", "audio": self.router.stats()})
        return web.json_response(self.status_payload())

    async def handle_api_qr(self, request: web.Request) -> web.Response:
        target = request.query.get("target", "pages")
        host = f"{self.primary_ip}:{self.port}"
        url = f"{self.pages_url}?host={host}" if target == "pages" else f"https://{host}/"
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml")

    async def handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(DOCS_DIR / "index.html")

    def build_https_app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("OPTIONS", "/{tail:.*}", self.handle_options)
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/trust", self.handle_trust)
        app.router.add_get("/ca.crt", self.handle_ca_cert)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/", self.handle_index)
        app.router.add_static("/", DOCS_DIR, show_index=False)
        return app

    def build_dashboard_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_pc_index)
        app.router.add_get("/api/status", self.handle_api_status)
        app.router.add_get("/api/qr.svg", self.handle_api_qr)
        app.router.add_post("/api/device", self.handle_api_device)
        app.router.add_post("/api/control", self.handle_api_control)
        app.router.add_static("/assets", DOCS_DIR, show_index=False)
        return app


async def run_server(args: argparse.Namespace) -> None:
    bridge = MicBridgeServer(
        port=args.port,
        dashboard_port=args.dashboard_port,
        pages_url=args.pages_url,
        device_index=args.device,
    )
    srv_cert, srv_key, ca_cert = ensure_certificates(CERTS_DIR, extra_ips=bridge.local_ips)
    bridge.ca_cert_path = ca_cert
    bridge.start_audio()

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(str(srv_cert), str(srv_key))

    https_runner = web.AppRunner(bridge.build_https_app())
    await https_runner.setup()
    https_site = web.TCPSite(https_runner, host="0.0.0.0", port=args.port, ssl_context=ssl_ctx)
    await https_site.start()

    dash_runner = web.AppRunner(bridge.build_dashboard_app())
    await dash_runner.setup()
    dash_site = web.TCPSite(dash_runner, host="127.0.0.1", port=args.dashboard_port)
    await dash_site.start()

    dash_url = f"http://127.0.0.1:{args.dashboard_port}/"
    status = bridge.status_payload()
    print("=" * 64)
    print("  iPhone 14 Pro -> Windows Microphone Bridge запущен!")
    print(f"  Панель управления на ПК (с QR-кодом): {dash_url}")
    print(f"  GitHub Pages для iPhone: {status['pagesUrl']}")
    print(f"  Прямой адрес в локальной сети: {status['localUrl']}")
    audio_stats = status["audio"]
    if audio_stats["active"]:
        print(f"  Вывод звука: {audio_stats['deviceName']} ({audio_stats['hostApi']}, {audio_stats['outputSampleRate']} Гц)")
    else:
        print(f"  [!] ВНИМАНИЕ: {audio_stats['error']}")
    print("=" * 64)
    print("Нажмите Ctrl+C для остановки.")

    if not args.no_browser:
        webbrowser.open(dash_url)

    try:
        while True:
            await asyncio.sleep(1.0)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        bridge.stop_audio()
        await https_runner.cleanup()
        await dash_runner.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="iPhone 14 Pro -> Windows Microphone Bridge")
    parser.add_argument("--port", type=int, default=8443, help="HTTPS/WSS port (default: 8443)")
    parser.add_argument("--dashboard-port", type=int, default=8780, help="Local PC dashboard HTTP port (default: 8780)")
    parser.add_argument("--pages-url", type=str, default=DEFAULT_PAGES_URL, help="GitHub Pages base URL")
    parser.add_argument("--device", type=int, default=None, help="Override sounddevice output index")
    parser.add_argument("--no-browser", action="store_true", help="Do not open PC dashboard automatically")
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(run_server(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
