"""Low-latency jitter-buffered audio output router for VB-Cable / Windows playback."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import sounddevice as sd


@dataclass(frozen=True)
class OutputDeviceInfo:
    index: int
    name: str
    hostapi_name: str
    max_output_channels: int
    default_samplerate: int
    is_vbcable: bool


def list_output_devices() -> list[OutputDeviceInfo]:
    """Return all available Windows playback devices."""
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    result: list[OutputDeviceInfo] = []
    for idx, dev in enumerate(devices):
        if int(dev.get("max_output_channels", 0)) <= 0:
            continue
        host_idx = int(dev.get("hostapi", 0))
        host_name = str(hostapis[host_idx]["name"]) if host_idx < len(hostapis) else ""
        name = str(dev["name"])
        lower = name.lower()
        is_vb = ("cable input" in lower) or ("vb-audio" in lower)
        result.append(
            OutputDeviceInfo(
                index=idx,
                name=name,
                hostapi_name=host_name,
                max_output_channels=int(dev["max_output_channels"]),
                default_samplerate=int(round(float(dev.get("default_samplerate", 48000)))),
                is_vbcable=is_vb,
            )
        )
    return result


def pick_best_vbcable_device(devices: Optional[list[OutputDeviceInfo]] = None) -> Optional[OutputDeviceInfo]:
    """Pick the best VB-Cable output device, preferring WASAPI then DirectSound then MME."""
    devs = devices if devices is not None else list_output_devices()
    vb_devs = [d for d in devs if d.is_vbcable]
    if not vb_devs:
        return None

    def _rank(d: OutputDeviceInfo) -> tuple[int, int]:
        h = d.hostapi_name.lower()
        if "wasapi" in h:
            api_rank = 0
        elif "directsound" in h:
            api_rank = 1
        elif "mme" in h:
            api_rank = 2
        else:
            api_rank = 3
        return (api_rank, d.index)

    return min(vb_devs, key=_rank)


def resample_mono_float32(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Fast linear resampler for 1D float32 audio frames."""
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    dst_len = max(1, int(round(len(samples) * (dst_rate / src_rate))))
    src_x = np.linspace(0.0, 1.0, num=len(samples), endpoint=False, dtype=np.float64)
    dst_x = np.linspace(0.0, 1.0, num=dst_len, endpoint=False, dtype=np.float64)
    return np.interp(dst_x, src_x, samples).astype(np.float32)


class AudioRingBuffer:
    """Thread-safe mono float32 ring buffer with bounded latency."""

    def __init__(self, capacity_frames: int) -> None:
        self._buf = np.zeros(capacity_frames, dtype=np.float32)
        self._cap = capacity_frames
        self._read = 0
        self._write = 0
        self._size = 0
        self._lock = threading.Lock()
        self.overruns = 0
        self.underruns = 0

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    def clear(self) -> None:
        with self._lock:
            self._read = 0
            self._write = 0
            self._size = 0

    def push(self, data: np.ndarray) -> None:
        n = len(data)
        if n == 0:
            return
        with self._lock:
            if n >= self._cap:
                # Keep only the newest capacity_frames samples.
                data = data[-self._cap :]
                n = self._cap
                self._read = 0
                self._write = 0
                self._size = 0
                self.overruns += 1
            elif self._size + n > self._cap:
                # Drop oldest frames to prevent unbounded latency buildup.
                drop = (self._size + n) - self._cap
                self._read = (self._read + drop) % self._cap
                self._size -= drop
                self.overruns += 1

            first = min(n, self._cap - self._write)
            self._buf[self._write : self._write + first] = data[:first]
            rem = n - first
            if rem > 0:
                self._buf[0:rem] = data[first:]
            self._write = (self._write + n) % self._cap
            self._size += n

    def pop(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        with self._lock:
            avail = min(n, self._size)
            if avail < n:
                self.underruns += 1
            if avail == 0:
                return out
            first = min(avail, self._cap - self._read)
            out[:first] = self._buf[self._read : self._read + first]
            rem = avail - first
            if rem > 0:
                out[first:avail] = self._buf[0:rem]
            self._read = (self._read + avail) % self._cap
            self._size -= avail
        return out


class VirtualMicRouter:
    """Routes incoming Int16 PCM chunks from iPhone into a Windows playback device (VB-Cable)."""

    def __init__(
        self,
        device_index: Optional[int] = None,
        max_buffer_ms: int = 120,
    ) -> None:
        self._lock = threading.Lock()
        self._stream: Optional[sd.OutputStream] = None
        self._device_index = device_index
        self._device_info: Optional[OutputDeviceInfo] = None
        self._out_rate = 48000
        self._out_channels = 2
        self._in_rate = 48000
        self._max_buffer_ms = max_buffer_ms
        self._ring = AudioRingBuffer(int(48000 * max_buffer_ms / 1000))
        self.rms_level = 0.0
        self.muted = False
        self.gain = 1.0
        self.last_error: Optional[str] = None

    @property
    def device_info(self) -> Optional[OutputDeviceInfo]:
        return self._device_info

    @property
    def output_samplerate(self) -> int:
        return self._out_rate

    def start(self, input_samplerate: int = 48000, device_index: Optional[int] = None) -> None:
        with self._lock:
            if device_index is not None:
                self._device_index = device_index
            self._in_rate = input_samplerate
            self._open_stream_locked()

    def stop(self) -> None:
        with self._lock:
            self._close_stream_locked()

    def set_input_samplerate(self, sample_rate: int) -> None:
        with self._lock:
            if sample_rate <= 0 or sample_rate == self._in_rate:
                return
            self._in_rate = sample_rate
            if self._stream is not None:
                self._open_stream_locked()

    def set_device(self, device_index: int) -> None:
        with self._lock:
            self._device_index = device_index
            self._open_stream_locked()

    def _close_stream_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _resolve_device_locked(self) -> Optional[OutputDeviceInfo]:
        devices = list_output_devices()
        if self._device_index is not None:
            for d in devices:
                if d.index == self._device_index:
                    return d
        best_vb = pick_best_vbcable_device(devices)
        if best_vb is not None:
            return best_vb
        return None

    def _open_stream_locked(self) -> None:
        self._close_stream_locked()
        self.last_error = None
        dev = self._resolve_device_locked()
        self._device_info = dev
        if dev is None:
            self.last_error = "VB-Cable (CABLE Input) не найден. Установите драйвер VB-Audio Virtual Cable."
            return

        channels = min(2, max(1, dev.max_output_channels))
        candidate_rates = [self._in_rate, dev.default_samplerate, 48000, 44100]
        seen: set[int] = set()
        rates_to_try = [r for r in candidate_rates if r > 0 and not (r in seen or seen.add(r))]

        last_exc: Optional[Exception] = None
        for rate in rates_to_try:
            try:
                sd.check_output_settings(
                    device=dev.index,
                    channels=channels,
                    samplerate=rate,
                    dtype="float32",
                )
                self._out_rate = rate
                self._out_channels = channels
                self._ring = AudioRingBuffer(max(2048, int(rate * self._max_buffer_ms / 1000)))

                def _callback(outdata: np.ndarray, frames: int, time_info, status) -> None:
                    mono = self._ring.pop(frames)
                    if self.muted:
                        outdata.fill(0.0)
                        return
                    if self._out_channels == 1:
                        outdata[:, 0] = mono
                    else:
                        for ch in range(self._out_channels):
                            outdata[:, ch] = mono

                stream = sd.OutputStream(
                    device=dev.index,
                    channels=channels,
                    samplerate=rate,
                    dtype="float32",
                    blocksize=256,
                    latency="low",
                    callback=_callback,
                )
                stream.start()
                self._stream = stream
                self._device_index = dev.index
                return
            except Exception as exc:
                last_exc = exc

        self.last_error = f"Не удалось открыть аудиоустройство {dev.name}: {last_exc}"

    def push_pcm_int16(self, raw_bytes: bytes) -> None:
        if not raw_bytes or len(raw_bytes) < 2:
            return
        usable = len(raw_bytes) - (len(raw_bytes) % 2)
        pcm = np.frombuffer(raw_bytes[:usable], dtype="<i2").astype(np.float32) / 32768.0
        if self.gain != 1.0:
            pcm = np.clip(pcm * self.gain, -1.0, 1.0)

        rms = float(np.sqrt(np.mean(pcm * pcm))) if len(pcm) else 0.0
        # Smooth RMS slightly for UI meter.
        self.rms_level = 0.65 * self.rms_level + 0.35 * rms

        if self._stream is None:
            return
        if self._in_rate != self._out_rate:
            pcm = resample_mono_float32(pcm, self._in_rate, self._out_rate)
        self._ring.push(pcm)

    def stats(self) -> dict:
        dev = self._device_info
        return {
            "active": self._stream is not None,
            "deviceIndex": dev.index if dev else None,
            "deviceName": dev.name if dev else None,
            "hostApi": dev.hostapi_name if dev else None,
            "isVbCable": dev.is_vbcable if dev else False,
            "inputSampleRate": self._in_rate,
            "outputSampleRate": self._out_rate,
            "bufferFrames": self._ring.size,
            "bufferMs": round(self._ring.size * 1000 / max(1, self._out_rate), 1),
            "rms": round(self.rms_level, 4),
            "overruns": self._ring.overruns,
            "underruns": self._ring.underruns,
            "muted": self.muted,
            "gain": self.gain,
            "error": self.last_error,
        }
