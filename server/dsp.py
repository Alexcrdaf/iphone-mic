"""Real-time low-latency voice DSP pipeline: HPF, adaptive spectral denoiser, AGC, and soft limiter."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np
from scipy import signal


@dataclass(frozen=True)
class DenoisePreset:
    oversub: float
    floor: float
    gate_thresh: float


DENOISE_PRESETS: dict[str, DenoisePreset] = {
    "low": DenoisePreset(oversub=1.1, floor=0.25, gate_thresh=0.0025),
    "medium": DenoisePreset(oversub=1.8, floor=0.12, gate_thresh=0.0040),
    "high": DenoisePreset(oversub=2.6, floor=0.05, gate_thresh=0.0065),
}


class VoiceDspProcessor:
    """Stateful mono float32 processor designed for 48 kHz / 44.1 kHz real-time microphone audio."""

    FFT_SIZE = 512
    HOP_SIZE = 256

    def __init__(self, sample_rate: int = 48000) -> None:
        self._lock = threading.Lock()
        self.sample_rate = max(8000, int(sample_rate))

        # User-tunable parameters
        self.gain: float = 2.2
        self.noise_mode: str = "medium"  # "off" | "low" | "medium" | "high"
        self.hpf_enabled: bool = True
        self.agc_enabled: bool = True

        # Telemetry
        self.input_rms: float = 0.0
        self.output_rms: float = 0.0
        self.agc_current_gain: float = 1.0
        self.calibrating_until: float = 0.0
        self.noise_calibrated: bool = False

        # Internal state
        self._sos = self._make_hpf(self.sample_rate)
        self._hpf_zi = np.zeros((self._sos.shape[0], 2), dtype=np.float64)

        self._win = signal.windows.hann(self.FFT_SIZE, sym=False).astype(np.float32)
        self._in_prev = np.zeros(self.HOP_SIZE, dtype=np.float32)
        self._ola_tail = np.zeros(self.HOP_SIZE, dtype=np.float32)
        self._in_queue = np.zeros(0, dtype=np.float32)
        self._out_queue = np.zeros(0, dtype=np.float32)

        n_bins = self.FFT_SIZE // 2 + 1
        self._noise_psd = np.full(n_bins, 1e-6, dtype=np.float32)
        self._prev_gain = np.ones(n_bins, dtype=np.float32)
        self._calib_accum = np.zeros(n_bins, dtype=np.float32)
        self._calib_frames = 0

        self._gate_env = 1.0
        self._agc_rms = 0.12

    @staticmethod
    def _make_hpf(sample_rate: int) -> np.ndarray:
        cutoff = min(85.0, sample_rate * 0.4)
        return signal.butter(2, cutoff, btype="highpass", fs=sample_rate, output="sos")

    def set_sample_rate(self, sample_rate: int) -> None:
        sr = max(8000, int(sample_rate))
        with self._lock:
            if sr == self.sample_rate:
                return
            self.sample_rate = sr
            self._sos = self._make_hpf(sr)
            self._hpf_zi = np.zeros((self._sos.shape[0], 2), dtype=np.float64)
            self._reset_stft_locked()

    def _reset_stft_locked(self) -> None:
        self._in_prev.fill(0.0)
        self._ola_tail.fill(0.0)
        self._in_queue = np.zeros(0, dtype=np.float32)
        self._out_queue = np.zeros(0, dtype=np.float32)
        self._prev_gain.fill(1.0)

    def start_noise_calibration(self, duration_sec: float = 1.5) -> None:
        with self._lock:
            self._calib_accum.fill(0.0)
            self._calib_frames = 0
            self.calibrating_until = time.monotonic() + max(0.3, duration_sec)

    def set_config(
        self,
        *,
        gain: float | None = None,
        noise_mode: str | None = None,
        hpf_enabled: bool | None = None,
        agc_enabled: bool | None = None,
    ) -> None:
        with self._lock:
            if gain is not None:
                self.gain = max(0.2, min(8.0, float(gain)))
            if noise_mode is not None and noise_mode in ("off", "low", "medium", "high"):
                if noise_mode != self.noise_mode and noise_mode == "off":
                    self._reset_stft_locked()
                self.noise_mode = noise_mode
            if hpf_enabled is not None:
                if hpf_enabled != self.hpf_enabled:
                    self._hpf_zi.fill(0.0)
                self.hpf_enabled = bool(hpf_enabled)
            if agc_enabled is not None:
                self.agc_enabled = bool(agc_enabled)

    def _stft_denoise_locked(self, samples: np.ndarray) -> np.ndarray:
        n = len(samples)
        if n == 0:
            return samples

        self._in_queue = np.concatenate([self._in_queue, samples])
        out_chunks: list[np.ndarray] = []
        now = time.monotonic()
        calibrating = now < self.calibrating_until
        preset = DENOISE_PRESETS.get(self.noise_mode, DENOISE_PRESETS["medium"])

        while len(self._in_queue) >= self.HOP_SIZE:
            hop_in = self._in_queue[: self.HOP_SIZE]
            self._in_queue = self._in_queue[self.HOP_SIZE :]

            frame = np.concatenate([self._in_prev, hop_in])
            self._in_prev = hop_in.copy()

            spec = np.fft.rfft(frame * self._win)
            power = (spec.real * spec.real + spec.imag * spec.imag).astype(np.float32)

            if calibrating:
                self._calib_accum += power
                self._calib_frames += 1
                self._noise_psd = np.maximum(self._calib_accum / max(1, self._calib_frames), 1e-8)
                self.noise_calibrated = True
            else:
                if self._calib_frames > 0:
                    self._calib_frames = 0
                # Adaptive noise tracking: fast downward tracking, gentle upward tracking on quiet bins.
                quieter = power < self._noise_psd
                near_noise = power < (self._noise_psd * 2.4)
                alpha = np.where(quieter, 0.14, np.where(near_noise, 0.035, 0.002)).astype(np.float32)
                self._noise_psd = (1.0 - alpha) * self._noise_psd + alpha * power
                self._noise_psd = np.maximum(self._noise_psd, 1e-8)

            snr = np.maximum(power / self._noise_psd - preset.oversub, 0.0)
            wiener = snr / (snr + 1.0)
            gain_spec = np.maximum(wiener, preset.floor).astype(np.float32)

            # 3-tap frequency smoothing + temporal smoothing prevents musical noise artifacts.
            padded = np.pad(gain_spec, (1, 1), mode="edge")
            freq_smoothed = 0.2 * padded[:-2] + 0.6 * padded[1:-1] + 0.2 * padded[2:]
            gain_spec = 0.55 * self._prev_gain + 0.45 * freq_smoothed
            self._prev_gain = gain_spec

            cleaned = np.fft.irfft(spec * gain_spec, n=self.FFT_SIZE).astype(np.float32)
            # Periodic Hann window at 50% overlap satisfies COLA sum == 1.0 without synthesis window.
            hop_out = self._ola_tail + cleaned[: self.HOP_SIZE]
            self._ola_tail = cleaned[self.HOP_SIZE :].copy()
            out_chunks.append(hop_out)

        if out_chunks:
            self._out_queue = np.concatenate([self._out_queue, *out_chunks])

        if len(self._out_queue) >= n:
            result = self._out_queue[:n]
            self._out_queue = self._out_queue[n:]
            return result

        # Initial priming block (< 1 hop): pad seamlessly with silence.
        avail = len(self._out_queue)
        result = np.zeros(n, dtype=np.float32)
        if avail > 0:
            result[n - avail :] = self._out_queue
            self._out_queue = np.zeros(0, dtype=np.float32)
        return result

    @staticmethod
    def _soft_limit(samples: np.ndarray) -> np.ndarray:
        """Linear below 0.70, smoothly compressed ceiling at 0.96 to prevent harsh clipping."""
        abs_x = np.abs(samples)
        knee = 0.70
        headroom = 0.26
        over = abs_x > knee
        if not np.any(over):
            return samples
        out = samples.copy()
        excess = (abs_x[over] - knee) / headroom
        compressed = knee + headroom * np.tanh(excess)
        out[over] = np.sign(samples[over]) * compressed
        return out

    def process(self, samples: np.ndarray) -> np.ndarray:
        if len(samples) == 0:
            return samples

        with self._lock:
            x = samples.astype(np.float32, copy=False)
            in_rms = float(np.sqrt(np.mean(x * x)))
            self.input_rms = 0.65 * self.input_rms + 0.35 * in_rms

            if self.hpf_enabled:
                y64, self._hpf_zi = signal.sosfilt(self._sos, x.astype(np.float64), zi=self._hpf_zi)
                x = y64.astype(np.float32)

            if self.noise_mode != "off":
                x = self._stft_denoise_locked(x)
                preset = DENOISE_PRESETS[self.noise_mode]
                block_rms = float(np.sqrt(np.mean(x * x)))
                target_gate = 1.0 if block_rms >= preset.gate_thresh else 0.18
                coeff = 0.35 if target_gate > self._gate_env else 0.08
                self._gate_env = (1.0 - coeff) * self._gate_env + coeff * target_gate
                x = x * self._gate_env

            if self.agc_enabled:
                block_rms = float(np.sqrt(np.mean(x * x)))
                # Only adapt AGC when speech is present (> -48 dBFS) so silence is not boosted.
                if block_rms > 0.004:
                    alpha = 0.03 if block_rms > self._agc_rms else 0.01
                    self._agc_rms = (1.0 - alpha) * self._agc_rms + alpha * block_rms
                target_rms = 0.16
                desired_agc = max(0.7, min(4.5, target_rms / max(self._agc_rms, 0.02)))
                self.agc_current_gain = 0.9 * self.agc_current_gain + 0.1 * desired_agc
                x = x * self.agc_current_gain
            else:
                self.agc_current_gain = 1.0

            if self.gain != 1.0:
                x = x * self.gain

            x = self._soft_limit(x)
            out_rms = float(np.sqrt(np.mean(x * x)))
            self.output_rms = 0.65 * self.output_rms + 0.35 * out_rms
            return x

    def stats(self) -> dict:
        with self._lock:
            return {
                "gain": round(self.gain, 2),
                "noiseMode": self.noise_mode,
                "hpfEnabled": self.hpf_enabled,
                "agcEnabled": self.agc_enabled,
                "agcCurrentGain": round(self.agc_current_gain, 2),
                "inputRms": round(self.input_rms, 4),
                "outputRms": round(self.output_rms, 4),
                "calibrating": time.monotonic() < self.calibrating_until,
                "noiseCalibrated": self.noise_calibrated,
            }
