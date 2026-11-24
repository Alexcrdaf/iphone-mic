from __future__ import annotations

import ssl
import tempfile
import unittest
from pathlib import Path

import numpy as np
from aiohttp.test_utils import AioHTTPTestCase
from cryptography import x509

from server.app import MicBridgeServer
from server.audio_out import AudioRingBuffer, resample_mono_float32
from server.certs import ensure_certificates


class CertsAndAudioUnitTests(unittest.TestCase):
    def test_ensure_certificates_creates_ca_and_san(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            srv_cert, srv_key, ca_cert = ensure_certificates(cert_dir, extra_ips=["192.168.1.101"])
            self.assertTrue(srv_cert.exists())
            self.assertTrue(srv_key.exists())
            self.assertTrue(ca_cert.exists())

            cert_obj = x509.load_pem_x509_certificate(srv_cert.read_bytes())
            san = cert_obj.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
            self.assertIn("127.0.0.1", ips)
            self.assertIn("192.168.1.101", ips)

            # Adding a new IP regenerates the server cert to include it.
            srv_cert2, _, _ = ensure_certificates(cert_dir, extra_ips=["192.168.1.101", "10.0.0.55"])
            cert_obj2 = x509.load_pem_x509_certificate(srv_cert2.read_bytes())
            san2 = cert_obj2.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            ips2 = {str(ip) for ip in san2.get_values_for_type(x509.IPAddress)}
            self.assertIn("10.0.0.55", ips2)

    def test_ring_buffer_bounded_and_underrun_safe(self) -> None:
        rb = AudioRingBuffer(capacity_frames=8)
        rb.push(np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32))
        self.assertEqual(rb.size, 4)
        out = rb.pop(2)
        np.testing.assert_allclose(out, [1.0, 2.0])

        # Push 10 samples into remaining space -> keeps newest 8, increments overruns.
        rb.push(np.arange(10, 20, dtype=np.float32))
        self.assertEqual(rb.size, 8)
        self.assertGreaterEqual(rb.overruns, 1)
        out2 = rb.pop(8)
        np.testing.assert_allclose(out2, np.arange(12, 20, dtype=np.float32))

        # Pop from empty buffer yields silence and increments underruns.
        empty = rb.pop(4)
        np.testing.assert_allclose(empty, np.zeros(4, dtype=np.float32))
        self.assertGreaterEqual(rb.underruns, 1)

    def test_resample_mono_float32(self) -> None:
        src = np.linspace(-1.0, 1.0, 441, dtype=np.float32)
        dst = resample_mono_float32(src, 44100, 48000)
        self.assertEqual(len(dst), 480)
        self.assertEqual(dst.dtype, np.float32)


class ServerIntegrationTests(AioHTTPTestCase):
    async def get_application(self):
        self.bridge = MicBridgeServer(port=8443, dashboard_port=8780)
        self._tmp_dir = tempfile.TemporaryDirectory()
        _, _, ca_cert = ensure_certificates(Path(self._tmp_dir.name), extra_ips=["127.0.0.1"])
        self.bridge.ca_cert_path = ca_cert
        return self.bridge.build_https_app()

    async def tearDownAsync(self) -> None:
        self._tmp_dir.cleanup()
        await super().tearDownAsync()

    async def test_health_trust_ca_and_websocket_pcm(self) -> None:
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["app"], "iphone-mic-pc")

        trust_resp = await self.client.get("/trust")
        self.assertEqual(trust_resp.status, 200)
        ca_resp = await self.client.get("/ca.crt")
        self.assertEqual(ca_resp.status, 200)
        self.assertIn(b"BEGIN CERTIFICATE", await ca_resp.read())

        async with self.client.ws_connect("/ws") as ws:
            initial = await ws.receive_json()
            self.assertEqual(initial["type"], "server_status")

            await ws.send_json({"type": "hello", "sampleRate": 48000, "mode": "wide", "micLabel": "iPhone 14 Pro"})
            ack = await ws.receive_json()
            self.assertEqual(ack["type"], "server_status")
            self.assertEqual(self.bridge.client_info["mode"], "wide")

            pcm = (np.ones(512, dtype=np.int16) * 1000).tobytes()
            await ws.send_bytes(pcm)
            await ws.send_json({"type": "ping", "t": 123.45})
            pong = await ws.receive_json()
            self.assertEqual(pong["type"], "pong")
            self.assertEqual(pong["t"], 123.45)
            self.assertGreaterEqual(self.bridge.client_info["packets"], 1)
            self.assertGreater(self.bridge.router.rms_level, 0.0)


class DashboardIntegrationTests(AioHTTPTestCase):
    async def get_application(self):
        self.bridge = MicBridgeServer(port=8443, dashboard_port=8780)
        return self.bridge.build_dashboard_app()

    async def test_dashboard_status_and_qr(self) -> None:
        resp = await self.client.get("/api/status")
        self.assertEqual(resp.status, 200)
        payload = await resp.json()
        self.assertTrue(payload["ok"])
        self.assertIn("pagesUrl", payload)

        qr_resp = await self.client.get("/api/qr.svg?target=pages")
        self.assertEqual(qr_resp.status, 200)
        svg = await qr_resp.text()
        self.assertIn("<svg", svg)


if __name__ == "__main__":
    unittest.main()
