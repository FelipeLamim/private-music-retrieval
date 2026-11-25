"use strict";
// Private Music Listening: interface do cliente confiável. Fala apenas com o processo cliente local
// (mesma origem); o áudio tocado vem só das respostas PIR decifradas por esse processo.
(function () {
  const token = new URLSearchParams(location.search).get("t") || "";
  const $ = (id) => document.getElementById(id);
  const audio = $("audio"), toggle = $("btn-toggle"), seek = $("seek");
  let info = null, job = null, pollTimer = null, playback = null, pollFailures = 0, seeking = false, lastPos = null;

  // ------------------------------------------------------------------ formatação (pt-BR)
  const nfCache = {};
  const num = (x, d) => (nfCache[d] || (nfCache[d] = new Intl.NumberFormat("pt-BR",
    { minimumFractionDigits: d, maximumFractionDigits: d }))).format(x);
  const ok = (x) => x != null && Number.isFinite(x);
  const fmtS = (s) => ok(s) ? num(s, s < 10 ? 2 : 1) + " s" : "—";
  const fmtMs = (ms) => !ok(ms) ? "—" : ms < 1000 ? num(ms, ms < 10 ? 1 : 0) + " ms" : num(ms / 1000, 2) + " s";
  const fmtBytes = (n) => !ok(n) ? "—" : n >= 1e6 ? num(n / 1e6, 2) + " MB" : n >= 1e3 ? num(n / 1e3, 1) + " kB" : n + " B";
  const fmtDur = (s) => { const r = Math.max(0, Math.floor(s || 0)); return Math.floor(r / 60) + ":" + String(r % 60).padStart(2, "0"); };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "X-PMR-Token": token }, opts.headers || {});
    return fetch(path, opts).then((r) => r.text().then((txt) => {
      let j = {};
      try { j = txt ? JSON.parse(txt) : {}; } catch (e) { j = {}; }
      if (!r.ok) { const err = new Error(j.error || ("HTTP " + r.status)); err.status = r.status; throw err; }
      return j;
    }));
  }

  // ------------------------------------------------------------------ telas (#sobre e #ouvir)
  function route(fromUser) {
    const player = location.hash === "#ouvir";
    $("view-landing").hidden = player;
    $("view-player").hidden = !player;
    $("nav-sobre").setAttribute("aria-current", player ? "false" : "page");
    $("nav-ouvir").setAttribute("aria-current", player ? "page" : "false");
    document.title = player ? "Ouvir · Private Music Listening" : "Private Music Listening";
    if (fromUser) {
      window.scrollTo(0, 0);
      (player ? $("player-title") : $("landing-title")).focus({ preventScroll: true });
    }
  }
  $("landing-title").setAttribute("tabindex", "-1");
  window.addEventListener("hashchange", () => route(true));
  route(false);

  function showBanner(text) {
    const b = $("app-error");
    b.textContent = text || "";
    b.hidden = !text;
  }

  // ------------------------------------------------------------------ catálogo
  function renderInfo() {
    const b = info.backend, l = info.layout, p = b.params;
    $("proto").textContent =
      `BFV N=${p.poly_modulus_degree}, t=${p.plain_modulus}, q=${p.coeff_modulus_bits.join("+")} bits (${p.security_level}) · ` +
      `${b.label} · ${l.num_records} faixas × ${l.num_blocks} blocos de ${l.block_bytes} B · política: ${info.client.policy}`;
    $("limit-policy").textContent = info.client.policy === "pad"
      ? `Toda recuperação pede os mesmos ${l.num_blocks} blocos, os da faixa mais longa, para não revelar a duração da faixa escolhida.`
      : "Esta sessão usa a política “stop”: a recuperação para no fim da faixa e revela a duração dela ao servidor.";
    const tracks = info.catalog.tracks;
    const tb = $("catalog").querySelector("tbody");
    tb.innerHTML = "";
    const status = $("catalog-status");
    if (!tracks.length) {
      status.textContent = "O catálogo está vazio: o servidor não publicou nenhuma faixa.";
      status.hidden = false;
      return;
    }
    status.hidden = true;
    tracks.forEach((t) => {
      const tr = document.createElement("tr");
      tr.id = "row-" + t.index;
      tr.innerHTML = `<td class="num">${t.index}</td><td>${esc(t.title)}</td><td class="col-artist">${esc(t.artist)}</td>` +
        `<td class="num">${fmtDur(t.duration_s)}</td><td class="act"></td>`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "Ouvir";
      btn.className = "btn btn-sm";
      btn.setAttribute("aria-label", `Ouvir ${t.title}`);
      btn.addEventListener("click", () => start(t.index));
      tr.lastChild.appendChild(btn);
      tb.appendChild(tr);
    });
    const licenses = [...new Set(tracks.map((t) => t.license))];
    const total = tracks.reduce((a, t) => a + (t.duration_s || 0), 0);
    $("catalog-summary").textContent = `${tracks.length} faixas · ${fmtDur(total)} no total` +
      (licenses.length === 1 ? ` · licença ${licenses[0]}` : "");
    renderMetrics(null);
    renderStats(null);
  }

  // ------------------------------------------------------------------ reprodução
  function resetPlayback() {
    lastPos = null;
    playback = { requested: performance.now(), startup_s: null, stalls: 0, stall_s: 0, waitingSince: null,
      ended: false, truncated: false, events: [] };
  }

  function detachAudio() {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }

  function start(index) {
    const mode = $("mode").value;
    detachAudio();
    document.querySelectorAll("#catalog tr.selected").forEach((r) => { r.classList.remove("selected"); r.removeAttribute("aria-current"); });
    const row = $("row-" + index);
    if (row) { row.classList.add("selected"); row.setAttribute("aria-current", "true"); }
    resetPlayback();
    job = null;
    const t = info.catalog.tracks[index];
    $("now").innerHTML = `${esc(t.title)}<span class="artist">${esc(t.artist)}</span>` +
      `<span class="tags">${mode === "progressive" ? "reprodução progressiva" : "reprodução completa"} · ` +
      `${fmtDur(t.duration_s)} · licença ${esc(t.license)}</span>`;
    setPlayState("retrieving", "Gerando a consulta cifrada…");
    $("progress-text").textContent = "recuperando · preparando a consulta";
    renderMetrics(null);
    if (window.matchMedia("(max-width: 900px)").matches) {  // em telas estreitas o tocador fica abaixo do catálogo
      const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      $("player-section").scrollIntoView({ block: "start", behavior: still ? "auto" : "smooth" });
    }
    api("/api/play", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ track: index, mode: mode }) })
      .then((j) => {
        job = j;
        $("btn-cancel").disabled = false;
        seek.max = String(j.audio_s_total || 0);
        $("time-total").textContent = fmtDur(j.audio_s_total);
        if (mode === "progressive") attachAudio();
        poll();
      })
      .catch((e) => {
        setPlayState("error", "Não foi possível iniciar a recuperação: " + e.message);
        $("progress-text").textContent = "erro · " + e.message;
      });
  }

  function attachAudio() {
    audio.src = `/api/jobs/${job.id}/audio.wav?t=${encodeURIComponent(token)}`;
    audio.volume = +$("volume").value;
    audio.play().catch(() => { /* sem ativação do usuário o navegador pode bloquear; o botão continua disponível */ });
    renderTransport();
  }

  // Uma recuperação cancelada ou com erro encerra o fluxo WAV antes do fim (o cabeçalho anuncia a faixa
  // inteira). Para o tocador quando todo o áudio decifrado já foi tocado: tudo está no buffer, o elemento não
  // está pausado e a posição não mudou desde a consulta anterior (1 s), ou o elemento falhou.
  function stopIfTruncated() {
    if (!job || !playback || playback.ended || job.state === "running" || job.state === "complete") return;
    if (!audio.getAttribute("src")) return;
    const n = audio.buffered.length, bufferedEnd = n ? audio.buffered.end(n - 1) : 0;
    const stuck = !audio.paused && lastPos === audio.currentTime;
    lastPos = audio.currentTime;
    if (!audio.error && !(stuck && bufferedEnd >= job.audio_s_available - 0.05)) return;
    detachAudio();
    playback.ended = true;
    playback.truncated = true;
    playback.events.push(["truncated", +now().toFixed(3), +bufferedEnd.toFixed(3)]);
    sendPlayback();
    renderTransport();
  }

  function poll() {
    clearTimeout(pollTimer);
    if (!job) return;
    const jid = job.id;
    api(`/api/jobs/${jid}`).then((s) => {
      if (!job || job.id !== jid) return;  // outra faixa foi escolhida enquanto a resposta chegava
      pollFailures = 0;
      showBanner("");
      job = s;
      renderProgress(s);
      renderMetrics(s);
      renderStats(s);
      stopIfTruncated();
      if (s.state === "complete" && s.mode === "complete" && !audio.getAttribute("src")) attachAudio();
      if (s.state === "running" || !playback.ended) pollTimer = setTimeout(poll, s.state === "running" ? 250 : 1000);
      if (s.state !== "running") $("btn-cancel").disabled = true;
      renderTransport();
      sendPlayback();
    }).catch(() => {
      pollFailures += 1;
      if (pollFailures >= 3) showBanner("Sem resposta do cliente local. Tentando de novo…");
      pollTimer = setTimeout(poll, 1000);
    });
  }

  function renderProgress(s) {
    const l = info.layout;
    const trackBlocks = Math.ceil((info.catalog.tracks[s.track].pcm_bytes || 0) / l.block_bytes);
    const pad = s.blocks_total - trackBlocks;
    let text;
    if (s.state === "running") {
      text = s.blocks_done === 0 ? "recuperando · consulta enviada, aguardando o primeiro bloco"
        : `recuperando · bloco ${s.blocks_done} de ${s.blocks_total} · ${fmtS(s.audio_s_available)} de áudio decifrados de ${fmtS(s.audio_s_total)}`;
    } else if (s.state === "complete") {
      text = `concluída · ${s.blocks_total} blocos em ${fmtS(s.elapsed_s)}` +
        (pad > 0 ? ` · ${pad} deles são preenchimento, que esconde a duração da faixa` : "");
    } else if (s.state === "cancelled") {
      text = `cancelada · ${s.blocks_done} de ${s.blocks_total} blocos recebidos`;
    } else {
      text = `erro · ${s.error || "falha na recuperação"}`;
    }
    $("progress-text").textContent = text;
    renderTimeline();
  }

  function renderTimeline() {
    const total = job ? job.audio_s_total : 0;
    const avail = job ? job.audio_s_available : 0;
    const pos = audio.getAttribute("src") ? audio.currentTime : 0;
    const pct = (x) => (total > 0 ? Math.min(100, (100 * x) / total) : 0).toFixed(2) + "%";
    seek.style.setProperty("--played-pct", pct(seeking ? +seek.value : pos));
    seek.style.setProperty("--avail-pct", pct(avail));
    if (!seeking) seek.value = String(pos);
    $("time-pos").textContent = fmtDur(seeking ? +seek.value : pos);
    $("time-total").textContent = fmtDur(total);
    $("time-avail").textContent = job && job.state === "running" && total > 0 ? `decifrado até ${fmtDur(avail)}` : "";
  }

  function setPlayState(state, text) {
    const el = $("play-state");
    el.dataset.state = state;
    el.textContent = text;
  }

  function renderTransport() {
    const hasSrc = !!audio.getAttribute("src");
    const playing = hasSrc && !audio.paused && !audio.ended;
    toggle.disabled = !hasSrc;
    toggle.classList.toggle("is-playing", playing);
    toggle.setAttribute("aria-label", playing ? "Pausar" : "Tocar");
    seek.disabled = !hasSrc;
    renderTimeline();
    if (!job && !playback) { setPlayState("idle", "Escolha uma faixa no catálogo."); return; }
    if (job && job.state === "error") { setPlayState("error", "A recuperação falhou: " + (job.error || "erro desconhecido")); return; }
    if (playback && playback.truncated) { setPlayState("idle", "Reprodução encerrada: a recuperação foi interrompida antes do fim."); return; }
    if (!hasSrc) {
      if (job && job.mode === "complete" && job.state === "running") {
        setPlayState("retrieving", "Recuperando a faixa inteira; a reprodução começa ao final (modo completo).");
      } else if (job && job.state === "cancelled") {
        setPlayState("idle", "Recuperação cancelada.");
      } else if (job && job.state === "running") {
        setPlayState("retrieving", "Recuperando…");
      }
      return;
    }
    if (audio.ended) setPlayState("idle", "Fim da faixa.");
    else if (audio.paused) setPlayState("idle", "Pausada.");
    else if (playback && playback.waitingSince != null) setPlayState("buffering", "Aguardando dados para continuar…");
    else if (playback && playback.startup_s == null) setPlayState("buffering", "Carregando o áudio…");
    else setPlayState("playing", "Tocando.");
  }

  // ------------------------------------------------------------------ métricas
  function metric(label, value, note, src) {
    const pending = value === "—" || value === "em andamento" || value === "ao final";
    return `<div class="metric"><span class="m-label">${esc(label)}</span>` +
      `<span class="m-value${pending ? " pending" : ""}">${esc(value)}</span>` +
      `<span class="m-note">${esc(note)}</span><span class="m-src">${esc(src)}</span></div>`;
  }

  function renderMetrics(s) {
    const r = (s && s.result) || {}, tm = r.timings_ms || {}, by = r.bytes || {};
    const running = !!(s && s.state === "running");
    const rate = s && s.elapsed_s > 0 ? s.audio_s_available / s.elapsed_s : null;
    const expansion = ok(by.wire_rx) && by.pcm ? by.wire_rx / by.pcm : null;
    $("metrics").innerHTML = [
      metric("Primeiro áudio", fmtS(s && s.first_block_s), "primeiro bloco decifrado", "medido no cliente"),
      metric("Início da reprodução", fmtS(playback && playback.startup_s), "do clique até o som", "medido no navegador"),
      metric("Recuperação completa", ok(tm.total_ms) ? fmtMs(tm.total_ms) : running ? "em andamento" : "—",
        s ? `${s.blocks_total} blocos` : "todos os blocos", "medido no cliente"),
      metric("Velocidade", ok(rate) ? num(rate, 1) + "×" : "—", "do tempo real: áudio recuperado por segundo", "calculado"),
      metric("Dados recebidos", ok(by.wire_rx) ? fmtBytes(by.wire_rx) : running ? "ao final" : "—",
        ok(expansion) ? `${num(expansion, 2)}× o tamanho do áudio (calculado)` : "respostas cifradas e cabeçalhos", "medido no cliente"),
      metric("Interrupções", playback && s ? String(playback.stalls) : "—",
        playback && s ? `${fmtS(playback.stall_s)} parado à espera de dados` : "paradas durante a reprodução", "medido no navegador"),
    ].join("");
    const chart = $("bytes-chart");
    if (ok(by.wire_rx) && ok(by.pcm) && by.wire_rx > 0) {
      const max = Math.max(by.wire_rx, by.pcm);
      $("bar-rx").style.width = (100 * by.wire_rx / max).toFixed(1) + "%";
      $("bar-pcm").style.width = (100 * by.pcm / max).toFixed(1) + "%";
      $("bar-rx-v").textContent = fmtBytes(by.wire_rx);
      $("bar-pcm-v").textContent = fmtBytes(by.pcm);
      chart.hidden = false;
      $("bytes-empty").hidden = true;
    } else {
      chart.hidden = true;
      $("bytes-empty").hidden = false;
    }
  }

  function renderStats(s) {
    const r = (s && s.result) || {}, tm = r.timings_ms || {}, by = r.bytes || {};
    const rate = s && s.elapsed_s > 0 ? s.audio_s_available / s.elapsed_s : null;
    const rows = [
      ["Primeiro bloco decifrado (TTFA)", fmtS(s && s.first_block_s), "cliente"],
      ["Início da reprodução no navegador", fmtS(playback && playback.startup_s), "navegador"],
      ["Interrupções / tempo parado", playback && s ? `${playback.stalls} / ${fmtS(playback.stall_s)}` : "—", "navegador"],
      ["Geração da consulta", fmtMs(tm.query_gen_ms), "cliente"],
      ["Envio da consulta", fmtMs(tm.query_upload_ms), "cliente"],
      ["Preparação da consulta no servidor", fmtMs(tm.server_prepare_ms), "servidor"],
      ["Processamento homomórfico no servidor", fmtMs(r.server_compute_ms), "servidor"],
      ["Decifração e decodificação no cliente", fmtMs(tm.client_decode_ms), "cliente"],
      ["Recuperação completa", fmtMs(tm.total_ms), "cliente"],
      ["Vazão (s de áudio por s)", ok(rate) ? num(rate, 1) + "×" : "—", "calculado"],
      ["Blocos recebidos", s ? `${s.blocks_done} de ${s.blocks_total}` : "—", "cliente"],
      ["Consulta enviada (corpo)", fmtBytes(by.query_body), "cliente"],
      ["Bytes enviados / recebidos (HTTP)", ok(by.wire_tx) ? `${fmtBytes(by.wire_tx)} / ${fmtBytes(by.wire_rx)}` : "—", "cliente"],
      ["Áudio PCM recuperado", fmtBytes(ok(by.pcm) ? by.pcm : s && s.pcm_done), "cliente"],
      ["Expansão (recebido ÷ áudio)", ok(by.wire_rx) && by.pcm ? num(by.wire_rx / by.pcm, 2) + "×" : "—", "calculado"],
      ["Chaves de sessão enviadas (uma vez)", fmtBytes(info.client.setup_bytes), "cliente"],
    ];
    $("stats").querySelector("tbody").innerHTML = rows.map(([k, v, o]) =>
      `<tr><td>${esc(k)}</td><td>${esc(v)}</td><td>${esc(o)}</td></tr>`).join("");
  }

  function sendPlayback() {
    if (!job || !playback) return;
    const body = { startup_s: playback.startup_s, stalls: playback.stalls, stall_s: playback.stall_s, ended: playback.ended,
      position_s: audio.currentTime, events: playback.events.slice(-50) };
    api(`/api/jobs/${job.id}/playback`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch(() => {});
  }

  function refreshWire() {
    api("/api/wire").then((w) => {
      $("wire").querySelector("tbody").innerHTML = w.entries.slice().reverse().slice(0, 14).map((e) =>
        `<tr><td>${esc(e.method)}</td><td class="mono">${esc(e.path)}</td><td class="num">${fmtBytes(e.request_bytes)}</td>` +
        `<td>${esc(e.status)}</td><td class="num">${fmtBytes(e.response_bytes)}</td></tr>`).join("");
    }).catch(() => {}).finally(() => { setTimeout(refreshWire, 1500); });
  }

  // ------------------------------------------------------------------ eventos do elemento audio
  const now = () => (performance.now() - playback.requested) / 1000;
  audio.addEventListener("playing", () => {
    if (!playback) return;
    if (playback.startup_s == null) playback.startup_s = now();
    if (playback.waitingSince != null) { playback.stall_s += now() - playback.waitingSince; playback.waitingSince = null; }
    playback.events.push(["playing", +now().toFixed(3), +audio.currentTime.toFixed(3)]);
    renderTransport();
    if (job) renderMetrics(job);
  });
  audio.addEventListener("waiting", () => {
    if (!playback || playback.startup_s == null || audio.ended) return;  // o buffer inicial é início, não parada
    playback.stalls += 1;
    playback.waitingSince = now();
    playback.events.push(["waiting", +now().toFixed(3), +audio.currentTime.toFixed(3)]);
    renderTransport();
  });
  audio.addEventListener("error", () => { stopIfTruncated(); renderTransport(); });
  audio.addEventListener("ended", () => {
    if (playback) { playback.ended = true; playback.events.push(["ended", +now().toFixed(3)]); sendPlayback(); }
    renderTransport();
  });
  ["play", "pause", "emptied", "loadedmetadata"].forEach((ev) => audio.addEventListener(ev, renderTransport));
  audio.addEventListener("timeupdate", renderTimeline);

  toggle.addEventListener("click", () => {
    if (!audio.getAttribute("src")) return;
    if (audio.paused || audio.ended) audio.play().catch(() => {});
    else audio.pause();
  });
  seek.addEventListener("input", () => { seeking = true; renderTimeline(); });
  seek.addEventListener("change", () => {
    const avail = job ? job.audio_s_available : 0;
    audio.currentTime = Math.max(0, Math.min(+seek.value, avail - 0.25));  // só dentro do que já foi decifrado
    seeking = false;
    renderTimeline();
  });
  $("volume").addEventListener("input", (e) => { audio.volume = +e.target.value; });
  $("btn-cancel").addEventListener("click", () => {
    if (!job) return;
    api(`/api/jobs/${job.id}/cancel`, { method: "POST" }).then(poll).catch(() => {});
  });

  // ------------------------------------------------------------------ início
  renderTransport();
  api("/api/catalog").then((j) => { info = j; renderInfo(); refreshWire(); })
    .catch((e) => {
      const msg = e.status === 403
        ? "Acesso negado: abra o endereço completo impresso pelo cliente no terminal; ele inclui o código de acesso desta execução."
        : "Não foi possível carregar o catálogo: " + e.message;
      showBanner(msg);
      const st = $("catalog-status");
      st.textContent = msg;
      st.classList.add("error");
      $("proto").textContent = msg;
    });
})();
