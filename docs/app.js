(() => {
  const $ = (id) => document.getElementById(id);

  const hostInput = $("hostInput");
  const checkHostBtn = $("checkHostBtn");
  const trustBanner = $("trustBanner");
  const trustLink = $("trustLink");
  const caLink = $("caLink");
  const hostStatusText = $("hostStatusText");
  const statusBadge = $("statusBadge");
  const pingPill = $("pingPill");

  const toggleMicBtn = $("toggleMicBtn");
  const muteBtn = $("muteBtn");
  const gainRange = $("gainRange");
  const gainValue = $("gainValue");
  const wakeLockToggle = $("wakeLockToggle");

  const meterFill = $("meterFill");
  const dbText = $("dbText");

  const modeGrid = $("modeGrid");
  const customControls = $("customControls");
  const ecToggle = $("ecToggle");
  const nsToggle = $("nsToggle");
  const agcToggle = $("agcToggle");
  const micPickerWrap = $("micPickerWrap");
  const micSelect = $("micSelect");

  const telRate = $("telRate");
  const telBuffer = $("telBuffer");
  const telDevice = $("telDevice");

  let currentMode = "voice";
  let isStreaming = false;
  let isMuted = false;
  let ws = null;
  let audioCtx = null;
  let mediaStream = null;
  let workletNode = null;
  let scriptNode = null;
  let gainNode = null;
  let analyserNode = null;
  let wakeLock = null;
  let meterRaf = null;
  let pingTimer = null;

  function initHost() {
    const params = new URLSearchParams(window.location.search);
    const paramHost = params.get("host");
    const savedHost = localStorage.getItem("iphone_mic_host");
    if (paramHost) {
      hostInput.value = paramHost;
      localStorage.setItem("iphone_mic_host", paramHost);
    } else if (window.location.hostname && !window.location.hostname.endsWith("github.io")) {
      const localHost = window.location.port
        ? `${window.location.hostname}:${window.location.port}`
        : window.location.hostname;
      hostInput.value = localHost;
      localStorage.setItem("iphone_mic_host", localHost);
    } else if (savedHost) {
      hostInput.value = savedHost;
    } else {
      hostInput.value = "192.168.1.101:8443";
    }
    updateTrustLinks();
  }

  function normalizedHost() {
    const raw = hostInput.value.trim().replace(/^https?:\/\//i, "").replace(/\/.*$/, "");
    if (!raw) return "";
    return raw.includes(":") ? raw : `${raw}:8443`;
  }

  function updateTrustLinks() {
    const host = normalizedHost();
    if (!host) return;
    trustLink.href = `https://${host}/trust`;
    caLink.href = `https://${host}/ca.crt`;
  }

  function setStatus(text, kind = "") {
    statusBadge.textContent = text;
    statusBadge.className = "badge" + (kind ? ` ${kind}` : "");
  }

  async function checkHostHealth() {
    const host = normalizedHost();
    if (!host) {
      hostStatusText.textContent = "Введите IP-адрес и порт ПК.";
      return false;
    }
    localStorage.setItem("iphone_mic_host", host);
    updateTrustLinks();
    hostStatusText.textContent = `Проверка связи с ${host}...`;

    try {
      const resp = await fetch(`https://${host}/health`, { cache: "no-store" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      trustBanner.classList.add("hidden");
      hostStatusText.textContent = data.vbcableReady
        ? `ПК на связи (${host}). VB-Cable готов!`
        : `ПК на связи (${host}), но VB-Cable не найден на ПК.`;
      return true;
    } catch (err) {
      trustBanner.classList.remove("hidden");
      hostStatusText.textContent = `Не удалось подключиться по HTTPS к ${host}. Если сервер на ПК запущен — нажмите «1. Доверить в Safari».`;
      return false;
    }
  }

  function getAudioConstraints() {
    let ec = true;
    let ns = true;
    let agc = true;

    if (currentMode === "wide") {
      ec = false;
      ns = false;
      agc = false;
    } else if (currentMode === "custom") {
      ec = ecToggle.checked;
      ns = nsToggle.checked;
      agc = agcToggle.checked;
    }

    const audio = {
      echoCancellation: ec,
      noiseSuppression: ns,
      autoGainControl: agc,
      channelCount: 1,
      sampleRate: { ideal: 48000 },
    };

    if (micSelect.value) {
      audio.deviceId = { exact: micSelect.value };
    }
    return { audio, video: false };
  }

  async function refreshMicDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const inputs = devices.filter((d) => d.kind === "audioinput");
      const currentVal = micSelect.value;
      micSelect.innerHTML = "";
      inputs.forEach((d, idx) => {
        const opt = document.createElement("option");
        opt.value = d.deviceId;
        opt.textContent = d.label || `Микрофон ${idx + 1}`;
        micSelect.appendChild(opt);
      });
      if (currentVal && [...micSelect.options].some((o) => o.value === currentVal)) {
        micSelect.value = currentVal;
      }
      if (inputs.length > 1) {
        micPickerWrap.classList.remove("hidden");
      }
    } catch (_) {
      // Ignore permission/enumeration errors before mic start.
    }
  }

  async function requestWakeLock() {
    if (!wakeLockToggle.checked || !("wakeLock" in navigator)) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
    } catch (_) {
      wakeLock = null;
    }
  }

  async function releaseWakeLock() {
    if (wakeLock) {
      try {
        await wakeLock.release();
      } catch (_) {}
      wakeLock = null;
    }
  }

  function startMeterLoop() {
    if (!analyserNode) return;
    const data = new Float32Array(analyserNode.fftSize);
    const tick = () => {
      if (!analyserNode || !isStreaming) return;
      analyserNode.getFloatTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        sum += data[i] * data[i];
      }
      const rms = Math.sqrt(sum / data.length);
      const db = rms > 1e-5 ? 20 * Math.log10(rms) : -100;
      const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
      meterFill.style.width = isMuted ? "0%" : `${pct.toFixed(1)}%`;
      dbText.textContent = isMuted ? "MUTE" : rms > 1e-5 ? `${db.toFixed(1)} дБ` : "-∞ дБ";
      meterRaf = requestAnimationFrame(tick);
    };
    meterRaf = requestAnimationFrame(tick);
  }

  function floatToInt16Buffer(floatArray) {
    const out = new Int16Array(floatArray.length);
    for (let i = 0; i < floatArray.length; i++) {
      let s = floatArray[i];
      if (s > 1) s = 1;
      else if (s < -1) s = -1;
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out.buffer;
  }

  async function startStreaming() {
    const host = normalizedHost();
    if (!host) {
      hostStatusText.textContent = "Укажите адрес ПК.";
      return;
    }
    localStorage.setItem("iphone_mic_host", host);
    updateTrustLinks();

    toggleMicBtn.disabled = true;
    setStatus("Подключение...", "");

    try {
      mediaStream = await navigator.mediaDevices.getUserMedia(getAudioConstraints());
      await refreshMicDevices();

      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      audioCtx = new AudioCtx({ latencyHint: "interactive", sampleRate: 48000 });
      if (audioCtx.state === "suspended") {
        await audioCtx.resume();
      }

      const source = audioCtx.createMediaStreamSource(mediaStream);
      gainNode = audioCtx.createGain();
      gainNode.gain.value = isMuted ? 0 : parseFloat(gainRange.value);

      analyserNode = audioCtx.createAnalyser();
      analyserNode.fftSize = 512;

      ws = new WebSocket(`wss://${host}/ws`);
      ws.binaryType = "arraybuffer";

      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("Таймаут подключения к ПК")), 6000);
        ws.onopen = () => {
          clearTimeout(timer);
          resolve();
        };
        ws.onerror = () => {
          clearTimeout(timer);
          reject(new Error("Ошибка WebSocket. Проверьте сертификат и брандмауэр."));
        };
      });

      const track = mediaStream.getAudioTracks()[0];
      const micLabel = track ? track.label : "iPhone Microphone";
      telRate.textContent = `${audioCtx.sampleRate} Гц`;

      ws.send(
        JSON.stringify({
          type: "hello",
          sampleRate: audioCtx.sampleRate,
          channels: 1,
          mode: currentMode,
          micLabel,
        })
      );

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "pong" && typeof msg.t === "number") {
            const rtt = Math.max(1, Math.round(performance.now() - msg.t));
            pingPill.textContent = `${rtt} мс`;
          }
          if (msg.audio) {
            telBuffer.textContent = `${msg.audio.bufferMs ?? 0} мс`;
            telDevice.textContent = msg.audio.deviceName || msg.audio.error || "Нет устройства";
          }
        } catch (_) {}
      };

      ws.onclose = () => {
        if (isStreaming) {
          stopStreaming("Соединение закрыто ПК");
        }
      };

      // Silence destination keeps iOS AudioContext clock alive without echoing mic to iPhone speaker.
      const silentGain = audioCtx.createGain();
      silentGain.gain.value = 0;

      source.connect(gainNode);
      gainNode.connect(analyserNode);

      if (audioCtx.audioWorklet) {
        await audioCtx.audioWorklet.addModule("./audio-processor.js");
        workletNode = new AudioWorkletNode(audioCtx, "pcm-capture-processor");
        workletNode.port.onmessage = (ev) => {
          if (ws && ws.readyState === WebSocket.OPEN && !isMuted) {
            ws.send(ev.data);
          }
        };
        gainNode.connect(workletNode);
        workletNode.connect(silentGain);
      } else {
        scriptNode = audioCtx.createScriptProcessor(512, 1, 1);
        scriptNode.onaudioprocess = (ev) => {
          if (ws && ws.readyState === WebSocket.OPEN && !isMuted) {
            const buf = floatToInt16Buffer(ev.inputBuffer.getChannelData(0));
            ws.send(buf);
          }
        };
        gainNode.connect(scriptNode);
        scriptNode.connect(silentGain);
      }
      silentGain.connect(audioCtx.destination);

      isStreaming = true;
      toggleMicBtn.textContent = "Остановить";
      toggleMicBtn.classList.add("stop");
      muteBtn.disabled = false;
      setStatus(isMuted ? "В эфире (Mute)" : "В эфире", isMuted ? "muted" : "live");
      trustBanner.classList.add("hidden");
      hostStatusText.textContent = `Передача звука на ${host}`;

      await requestWakeLock();
      startMeterLoop();
      pingTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping", t: performance.now() }));
        }
      }, 2000);
    } catch (err) {
      await stopStreaming();
      setStatus("Ошибка", "error");
      hostStatusText.textContent = err && err.message ? err.message : "Не удалось запустить микрофон";
      if (String(hostStatusText.textContent).includes("WebSocket")) {
        trustBanner.classList.remove("hidden");
      }
    } finally {
      toggleMicBtn.disabled = false;
    }
  }

  async function stopStreaming(reason = "") {
    isStreaming = false;
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
    if (meterRaf) {
      cancelAnimationFrame(meterRaf);
      meterRaf = null;
    }
    if (workletNode) {
      workletNode.disconnect();
      workletNode = null;
    }
    if (scriptNode) {
      scriptNode.disconnect();
      scriptNode = null;
    }
    if (gainNode) {
      gainNode.disconnect();
      gainNode = null;
    }
    if (analyserNode) {
      analyserNode.disconnect();
      analyserNode = null;
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach((t) => t.stop());
      mediaStream = null;
    }
    if (audioCtx) {
      try {
        await audioCtx.close();
      } catch (_) {}
      audioCtx = null;
    }
    if (ws) {
      try {
        ws.close();
      } catch (_) {}
      ws = null;
    }
    await releaseWakeLock();

    toggleMicBtn.textContent = "Включить микрофон";
    toggleMicBtn.classList.remove("stop");
    muteBtn.disabled = true;
    meterFill.style.width = "0%";
    dbText.textContent = "-∞ дБ";
    pingPill.textContent = "— мс";
    setStatus("Отключено", "");
    if (reason) {
      hostStatusText.textContent = reason;
    }
  }

  async function restartIfStreaming() {
    if (!isStreaming) return;
    await stopStreaming();
    await startStreaming();
  }

  checkHostBtn.addEventListener("click", checkHostHealth);

  toggleMicBtn.addEventListener("click", () => {
    if (isStreaming) stopStreaming();
    else startStreaming();
  });

  muteBtn.addEventListener("click", () => {
    isMuted = !isMuted;
    muteBtn.classList.toggle("active-mute", isMuted);
    muteBtn.textContent = isMuted ? "Unmute" : "Mute";
    if (gainNode) {
      gainNode.gain.value = isMuted ? 0 : parseFloat(gainRange.value);
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "config", muted: isMuted }));
    }
    if (isStreaming) {
      setStatus(isMuted ? "В эфире (Mute)" : "В эфире", isMuted ? "muted" : "live");
    }
  });

  gainRange.addEventListener("input", () => {
    const val = parseFloat(gainRange.value);
    gainValue.textContent = `${val.toFixed(1)}x`;
    if (gainNode && !isMuted) {
      gainNode.gain.value = val;
    }
  });

  wakeLockToggle.addEventListener("change", () => {
    if (isStreaming) {
      if (wakeLockToggle.checked) requestWakeLock();
      else releaseWakeLock();
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && isStreaming && wakeLockToggle.checked) {
      requestWakeLock();
    }
  });

  modeGrid.addEventListener("click", (e) => {
    const btn = e.target.closest(".mode-btn");
    if (!btn) return;
    currentMode = btn.dataset.mode;
    modeGrid.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("active", b === btn));
    customControls.classList.toggle("hidden", currentMode !== "custom");
    restartIfStreaming();
  });

  [ecToggle, nsToggle, agcToggle, micSelect].forEach((el) => {
    el.addEventListener("change", restartIfStreaming);
  });

  initHost();
})();
