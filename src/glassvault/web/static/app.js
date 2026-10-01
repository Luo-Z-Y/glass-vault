// Glass Vault demo UI. Plain JS, no build step.
// The server owns the conversation and the bank; this page renders what it returns,
// asks the device's passkey (Face ID / Touch ID) to sign, and speaks the bank's script.

const $ = (sel) => document.querySelector(sel);
const chat = $("#chat");
const input = $("#input");
const phone = $(".iphone");
const island = $("#island");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// [trace stage, label, path]. After the draft a request takes one path: a payment runs
// the payment checks, while a balance or transactions enquiry is answered from bank
// records and needs none of them.
const STAGES = [
  ["injection-scan", "Scam-script scan"],
  ["quarantine", "Untrusted text quarantined"],
  ["draft", "AI draft"],
  ["records", "Answered from bank records", "answer"],
  ["provenance", "Every detail traced to your words", "payment"],
  ["resolve", "Maths done in code", "payment"],
  ["validator", "Independent AI re-derivation", "payment"],
  ["policy", "Risk and KYC rules", "payment"],
  ["challenge", "Bank-rendered confirmation", "payment"],
  ["signature", "Your Face ID signature", "payment"],
];

let state = null;
let lastInputWasVoice = false;

// ---------------------------------------------------------------------------
// API

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || res.statusText), data);
  return data;
}

// ---------------------------------------------------------------------------
// Chat rendering

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function addMsg(cls, text) {
  const node = el("div", `msg ${cls}`, text);
  chat.appendChild(node);
  chat.scrollTop = chat.scrollHeight;
  return node;
}

function renderOutcome(o) {
  switch (o.kind) {
    case "clarify": {
      const node = addMsg("agent", o.message);
      for (const opt of o.clarification?.options || []) {
        const b = el("button", "suggest", opt.label);
        // Reply with the plain choice (a name or an amount), not the label's extra details
        // like "last paid S$32.00", which would otherwise count as amounts the customer said.
        const say = opt.label.replace(/\s*\(.*\)\s*$/, "").replace(/^Yes,\s*/i, "");
        b.addEventListener("click", () => (opt.value === "cancel" ? cancel() : send(say)));
        node.appendChild(b);
      }
      break;
    }
    case "answer":
      renderAnswer(o);
      break;
    case "confirm":
      renderConfirm(o);
      break;
    case "frozen": {
      const node = addMsg("frozen", o.message);
      node.appendChild(el("div", "why", "See why in the security console."));
      break;
    }
    case "held":
      addMsg("held", o.message);
      break;
    case "refused":
      addMsg("refused", o.message);
      break;
    default:
      addMsg("refused", o.message || "Something went wrong. Nothing has been sent.");
  }
  chat.scrollTop = chat.scrollHeight;
}

// A balance or transactions enquiry: bank records in the bank's fixed wording, one line each.
// Statement lines are "date  description  amount", two spaces apart (the bank collapses
// spaces inside a description), so they lay out as columns with the amounts lined up.
function renderAnswer(o) {
  const node = addMsg("agent answer");
  for (const line of o.answer?.lines || o.message.split("\n")) {
    const cells = line.split(/ {2,}/);
    const row = el("div", "answer-line");
    for (const cell of cells) row.appendChild(el("span", "", cell));
    node.appendChild(row);
    if (cells.length === 3) node.classList.add("statement");
  }
  if (lastInputWasVoice && o.answer?.spoken_script) speakBankVoice(o.answer.spoken_script, o.speech);
}

function renderConfirm(o) {
  const card = $("#confirm-tpl").content.firstElementChild.cloneNode(true);
  const n = o.step_count ?? o.confirmation.lines.length;
  card.querySelector(".confirm-title").textContent = `Confirm ${n} ${n === 1 ? "instruction" : "instructions"}`;
  const list = card.querySelector(".confirm-lines");
  // Payment steps only; the risk notices below come from the same policy decision.
  for (const line of o.confirmation.lines.slice(0, n)) list.appendChild(el("li", "", line));

  const policy = card.querySelector(".confirm-policy");
  for (const r of o.policy?.reasons || []) {
    const cls = o.policy.outcome === "step_up" ? "step-up" : "hold";
    policy.appendChild(el("div", `notice ${cls}`, r.message));
  }

  if (o.challenge?.step_up_required) {
    const box = el("div", "stepup");
    box.appendChild(el("label", "", "Extra check: enter the 6-digit code from your digital token"));
    const row = el("div", "stepup-row");
    const code = el("input");
    Object.assign(code, { inputMode: "numeric", maxLength: 6, placeholder: "••••••", autocomplete: "one-time-code" });
    const show = el("button", "ghost token", "Show token (simulated)");
    const shown = el("div", "token-code");
    shown.hidden = true;
    show.addEventListener("click", async () => {
      const { code: c } = await api(`/api/token/${o.challenge.nonce}`);
      shown.textContent = `Digital token: ${c}`;
      shown.hidden = false;
    });
    row.append(code, show);
    box.append(row, shown);
    policy.after(box);
  }

  const approve = card.querySelector(".approve");
  const usePasskey = state?.passkey?.registered;
  if (!usePasskey) approve.textContent = state?.passkey?.simulated_allowed ? "Approve (simulated Face ID)" : "Set up Face ID first";
  approve.disabled = !usePasskey && !state?.passkey?.simulated_allowed;
  approve.addEventListener("click", () => approveChallenge(o, card));
  card.querySelector(".listen").addEventListener("click", () => speakBankVoice(o.confirmation.spoken_script, o.speech));
  card.querySelector(".cancel").addEventListener("click", () => {
    card.remove();
    cancel();
  });

  const wrap = el("div", "msg agent");
  wrap.appendChild(el("div", "", o.message));
  wrap.appendChild(card);
  chat.appendChild(wrap);
  if (lastInputWasVoice) speakBankVoice(o.confirmation.spoken_script, o.speech);
}

// ---------------------------------------------------------------------------
// Security console

function renderStages(trace, { signature } = {}) {
  const list = $("#stages");
  list.innerHTML = "";
  const byStage = new Map(trace.map((t) => [t.stage, t]));
  if (signature) byStage.set("signature", signature);
  // The answer row shows only for answers, so a payment's checks read as before; for an
  // answer, the payment checks it had no need of say so.
  const answered = byStage.has("records");
  const stages = STAGES.filter(([, , path]) => path !== "answer" || answered);
  const failedAt = stages.findIndex(([key]) => byStage.get(key) && !byStage.get(key).ok);

  stages.forEach(([key, label, path], i) => {
    const t = byStage.get(key);
    const status = t ? (t.ok ? "ok" : "fail")
      : answered && path === "payment" ? "na"
      : failedAt !== -1 && i > failedAt ? "skip" : "pending";
    const li = el("li", status);
    li.appendChild(el("span", `dot ${status}`));
    const body = el("div", "stage-body");
    body.appendChild(el("div", "stage-name", label));
    const idle = { na: "not needed for a read-only answer", skip: "not reached", pending: "waiting" };
    body.appendChild(el("div", "detail", t ? t.detail : idle[status]));
    li.appendChild(body);
    if (!reduceMotion) {
      li.style.opacity = 0;
      setTimeout(() => (li.style.opacity = 1), 90 * i);
    }
    list.appendChild(li);
  });
}

function renderDiff(diff) {
  const box = $("#diff");
  box.innerHTML = "";
  if (!diff || !diff.length) {
    box.hidden = true;
    return;
  }
  box.appendChild(el("div", "diff-title", "Draft vs independent re-derivation"));
  for (const d of diff) box.appendChild(el("div", d.kind === "missing" ? "plus" : "minus", `${d.kind}: ${d.detail}`));
  box.hidden = false;
}

async function refreshAudit() {
  const { entries, intact } = await api("/api/audit");
  const chain = $("#chain");
  chain.textContent = intact ? `${entries.length} entries · chain intact` : "CHAIN BROKEN";
  chain.className = `pill ${intact ? "ok" : "bad"}`;
  const list = $("#audit");
  list.innerHTML = "";
  for (const e of entries.slice(-10).reverse()) {
    const li = el("li");
    li.appendChild(el("span", "kind", e.outcome ? `${e.kind} · ${e.outcome}` : e.kind));
    li.appendChild(el("span", "at", e.at.slice(11, 19)));
    li.appendChild(el("span", "hash", e.hash.slice(0, 12)));
    list.appendChild(li);
  }
}

// The LLM service panel: which model drafts and which one re-derives, live or offline.
function renderLLM(llm) {
  const status = $("#llm-status");
  status.textContent = llm.live ? "Live" : "Offline";
  status.className = `pill ${llm.live ? "ok" : ""}`;
  for (const role of ["drafter", "validator"]) {
    const { model, host } = llm[role];
    $(`#${role}-model`).textContent = llm.live ? model : "Offline stand-in";
    $(`#${role}-host`).textContent = llm.live ? host || "" : "rule-based, runs without an API key";
  }
}

// The voice panel: who speaks for the bank and who listens, TRTC or the browser.
function renderVoice(voice) {
  const speak = voice?.speak || {};
  const listen = voice?.listen || {};
  const live = speak.engine === "trtc" || listen.engine === "trtc";
  $("#voice-status").textContent = live ? "TRTC" : "Browser";
  $("#voice-status").className = `pill ${live ? "ok" : ""}`;
  $("#voice-speak").textContent = speak.engine === "trtc" ? speak.name : "Browser voice";
  $("#voice-listen").textContent = `Listening: ${listen.engine === "trtc" ? listen.name : "browser speech recognition"}`;
}

function renderState(s) {
  state = s;
  $("#customer-name").textContent = s.customer_name;
  const balances = $("#balances");
  balances.innerHTML = "";
  for (const a of s.accounts) {
    const chip = el("div", "balance");
    chip.appendChild(el("span", "", a.name.replace(" Account", "")));
    chip.appendChild(el("strong", "", `S$${Number(a.balance).toLocaleString("en-SG", { minimumFractionDigits: 2 })}`));
    balances.appendChild(chip);
  }
  renderLLM(s.llm);
  renderVoice(s.voice);
  $("#passkey-btn").textContent = s.passkey.registered ? "Face ID ready" : "Set up Face ID";
  $("#passkey-btn").classList.toggle("ok", s.passkey.registered);
  $("#rogue-toggle").checked = Boolean(s.rogue);
  $("#rogue-attack").disabled = !s.rogue;
  if (s.rogue) $("#rogue-attack").value = s.rogue;
  document.body.classList.toggle("rogue", Boolean(s.rogue));
  $("#blocked-count").textContent = s.attacks.blocked;
  $("#funds-moved").textContent = `S$${Number(s.attacks.funds_moved).toFixed(2)}`;
}

// ---------------------------------------------------------------------------
// Conversation

async function send(text) {
  text = text.trim();
  if (!text) return;
  addMsg("customer", text);
  input.value = "";
  renderStages([]);
  renderDiff(null);
  const typing = addMsg("agent typing", "Checking…");
  try {
    const { outcome, state: s } = await api("/api/message", { text, voice: lastInputWasVoice });
    typing.remove();
    renderState(s);
    renderOutcome(outcome);
    renderStages(outcome.trace);
    renderDiff(outcome.diff);
  } catch (err) {
    typing.remove();
    addMsg("refused", err.message || "Something went wrong. Nothing has been sent.");
  }
  refreshAudit();
}

async function cancel() {
  await api("/api/cancel", {});
  addMsg("system", "Cancelled. Nothing has been sent.");
}

async function approveChallenge(o, card) {
  const btn = card.querySelector(".approve");
  btn.disabled = true;
  const faceId = faceIdScan();
  try {
    let body = { nonce: o.challenge.nonce, step_up_code: card.querySelector(".stepup input")?.value || null };
    if (state.passkey.registered) {
      const cred = await navigator.credentials.get({ publicKey: requestOptions(o.webauthn) });
      body.assertion = assertionToJSON(cred);
    } else {
      body.simulated = true;
    }
    const { receipts, state: s } = await api("/api/approve", body);
    await faceId(true);
    card.querySelector(".confirm-actions").remove();
    const done = addMsg("done", "Done. Receipts:");
    for (const r of receipts) done.appendChild(el("div", "receipt", `${r.reference}  ${r.summary}`));
    renderState(s);
    const trace = [...o.trace, { stage: "signature", ok: true, detail: "gateway verified the signature over this exact plan" }];
    renderStages(trace);
  } catch (err) {
    await faceId(false);
    btn.disabled = false;
    const code = err.code ? `${err.status} ${err.code}` : err.name;
    addMsg("refused", `Not sent: ${code}. ${err.message || ""}`);
    renderStages(o.trace, { signature: { stage: "signature", ok: false, detail: code } });
  }
  refreshAudit();
}

// ---------------------------------------------------------------------------
// Passkeys (WebAuthn). The server's options use base64url for binary fields.

const b64urlToBuf = (s) => {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
};
const bufToB64url = (buf) =>
  btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function creationOptions(json) {
  const o = structuredClone(json);
  o.challenge = b64urlToBuf(o.challenge);
  o.user.id = b64urlToBuf(o.user.id);
  o.excludeCredentials = (o.excludeCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) }));
  return o;
}

function requestOptions(json) {
  const o = structuredClone(json);
  o.challenge = b64urlToBuf(o.challenge);
  o.allowCredentials = (o.allowCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) }));
  return o;
}

function assertionToJSON(cred) {
  return {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: bufToB64url(cred.response.clientDataJSON),
      authenticatorData: bufToB64url(cred.response.authenticatorData),
      signature: bufToB64url(cred.response.signature),
      userHandle: cred.response.userHandle ? bufToB64url(cred.response.userHandle) : null,
    },
  };
}

async function registerPasskey() {
  if (state?.passkey?.registered) return;
  try {
    const options = await api("/api/passkey/register/options", {});
    const cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
    const s = await api("/api/passkey/register/verify", {
      credential: {
        id: cred.id,
        rawId: bufToB64url(cred.rawId),
        type: cred.type,
        authenticatorAttachment: cred.authenticatorAttachment,
        clientExtensionResults: cred.getClientExtensionResults(),
        response: {
          clientDataJSON: bufToB64url(cred.response.clientDataJSON),
          attestationObject: bufToB64url(cred.response.attestationObject),
          transports: cred.response.getTransports ? cred.response.getTransports() : [],
        },
      },
    });
    renderState(s.state);
    addMsg("system", "Face ID is set up. Only this device can approve payments now.");
  } catch (err) {
    addMsg("system", `Face ID setup failed: ${err.code || err.name}. ${err.message || ""}`);
  }
  refreshAudit();
}

// ---------------------------------------------------------------------------
// iPhone chrome: the status bar clock and the Dynamic Island. Cosmetic only;
// the island never takes part in approving anything.

function tickClock() {
  const now = new Date();
  $("#sb-time").textContent = `${now.getHours() % 12 || 12}:${String(now.getMinutes()).padStart(2, "0")}`;
}

function setIsland(mode) {
  if (mode) island.dataset.mode = mode;
  else delete island.dataset.mode;
}

// Face ID scanning in the island while the approval is signed. Returns a function that
// shows the result (after a short minimum scan, so it reads on stage) and resolves once
// the result is visible; the island then shrinks back by itself.
function faceIdScan() {
  const started = performance.now();
  setIsland("scan");
  return async (ok) => {
    await sleep(Math.max(0, 800 - (performance.now() - started)));
    const mode = ok ? "ok" : "fail";
    setIsland(mode);
    setTimeout(() => island.dataset.mode === mode && setIsland(null), ok ? 1100 : 1300);
    await sleep(ok ? 450 : 300);
  };
}

// ---------------------------------------------------------------------------
// Voice: push-to-talk in, a fixed "bank voice" out. With TRTC set up on the server, the
// recording goes to TRTC speech recognition and the bank voice is TRTC text-to-speech;
// otherwise the browser's own speech recognition and voice are used.

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const canRecord = Boolean(navigator.mediaDevices?.getUserMedia && window.AudioWorkletNode);
const MAX_RECORDING_MS = 30_000;
const MIN_RECORDING_S = 0.3;
const INPUT_HINT = input.placeholder;
let trtcListenFailed = false; // after TRTC recognition fails, later presses use the browser's own (if any)

function setupMic() {
  const mic = $("#mic");
  if (!Recognition && !canRecord) {
    mic.hidden = true;
    return;
  }
  let session = null;
  let limit = null;
  let busy = false; // from release until the recording is transcribed and sent
  const listening = (on) => {
    mic.classList.toggle("listening", on);
    phone.classList.toggle("mic-on", on);
  };
  const end = () => {
    session = null;
    clearTimeout(limit);
    listening(false);
  };
  const stop = () => {
    if (!session) return;
    const current = session;
    end();
    busy = true;
    Promise.resolve(current.stop()).finally(() => (busy = false));
  };
  const start = (e) => {
    e.preventDefault();
    if (session || busy) return;
    const trtc = state?.voice?.listen?.engine === "trtc" && canRecord && !(trtcListenFailed && Recognition);
    if (!trtc && !Recognition) {
      addMsg("system", "Voice input needs TRTC speech recognition or a browser that can transcribe. You can type instead.");
      return;
    }
    const current = trtc
      ? recordForTrtc(() => session === current && listening(true)) // lit once the microphone is really on
      : listenInBrowser(() => session === current && end()); // recognition can end by itself
    session = current;
    if (!trtc) listening(true);
    limit = setTimeout(stop, MAX_RECORDING_MS);
  };
  mic.addEventListener("pointerdown", start);
  mic.addEventListener("pointerup", stop);
  mic.addEventListener("pointerleave", stop);
}

function listenInBrowser(onEnd) {
  const recognition = new Recognition();
  recognition.lang = "en-SG";
  recognition.interimResults = true;
  recognition.onresult = (ev) => {
    input.value = Array.from(ev.results).map((r) => r[0].transcript).join("");
  };
  recognition.onend = () => {
    onEnd();
    if (input.value.trim()) {
      lastInputWasVoice = true;
      send(input.value);
    }
  };
  recognition.start();
  return { stop: () => recognition.stop() };
}

// Records the microphone while the button is held, then sends 16 kHz mono WAV to the server,
// which has TRTC transcribe it. `onReady` runs once audio is actually being captured.
function recordForTrtc(onReady) {
  const chunks = [];
  let stream = null;
  let ctx = null;
  const ready = (async () => {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    ctx = new AudioContext();
    await ctx.audioWorklet.addModule("/static/pcm-recorder.js");
    const node = new AudioWorkletNode(ctx, "pcm-recorder");
    node.port.onmessage = (ev) => chunks.push(ev.data);
    ctx.createMediaStreamSource(stream).connect(node);
    node.connect(ctx.destination); // silent; keeps the recorder running
    await ctx.resume();
    onReady();
  })();
  return {
    async stop() {
      let rate = 0;
      try {
        await ready;
        rate = ctx.sampleRate;
      } catch (err) {
        addMsg("system", `The microphone is not available (${err.message}). You can type instead.`);
        return;
      } finally {
        stream?.getTracks().forEach((t) => t.stop());
        if (ctx && ctx.state !== "closed") ctx.close().catch(() => {});
      }
      const seconds = chunks.reduce((n, c) => n + c.length, 0) / rate;
      if (seconds < MIN_RECORDING_S) {
        addMsg("system", "Hold the mic while you speak, then let go.");
        return;
      }
      await transcribeWithTrtc(wav16k(chunks, rate));
    },
  };
}

async function transcribeWithTrtc(wav) {
  input.placeholder = "Transcribing…";
  try {
    const res = await fetch("/api/asr", { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.message || res.statusText), { status: res.status });
    if (data.text?.trim()) {
      lastInputWasVoice = true;
      send(data.text);
    } else {
      addMsg("system", "I didn't catch anything. Hold the mic and try again, or type.");
    }
  } catch (err) {
    // The service failed (not the recording): the browser listens from the next press, if it can.
    if (Recognition && (!err.status || err.status >= 500)) trtcListenFailed = true;
    const next = trtcListenFailed ? "Hold the mic again: this browser will listen instead." : "You can type instead.";
    addMsg("system", `Couldn't transcribe that (${err.message}). ${next}`);
  } finally {
    input.placeholder = INPUT_HINT;
  }
}

// Mono Float32 blocks at `rate` -> a 16 kHz, 16-bit PCM WAV file (averaging each output sample's window).
function wav16k(chunks, rate) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const samples = new Float32Array(total);
  let offset = 0;
  for (const c of chunks) {
    samples.set(c, offset);
    offset += c.length;
  }
  const ratio = rate / 16000;
  const pcm = new Int16Array(Math.floor(total / ratio));
  for (let i = 0; i < pcm.length; i++) {
    let v;
    if (ratio >= 1) {
      // Faster than 16 kHz: average the samples this output sample covers.
      const from = Math.floor(i * ratio);
      const to = Math.max(from + 1, Math.min(total, Math.floor((i + 1) * ratio)));
      let sum = 0;
      for (let j = from; j < to; j++) sum += samples[j];
      v = sum / (to - from);
    } else {
      // Slower (a narrowband headset): interpolate between the two nearest samples.
      const at = i * ratio;
      const j = Math.floor(at);
      v = samples[j] + (samples[Math.min(j + 1, total - 1)] - samples[j]) * (at - j);
    }
    v = Math.max(-1, Math.min(1, v));
    pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const ascii = (at, text) => [...text].forEach((ch, i) => view.setUint8(at + i, ch.charCodeAt(0)));
  ascii(0, "RIFF");
  view.setUint32(4, 36 + pcm.length * 2, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, 16000, true);
  view.setUint32(28, 32000, true); // bytes per second
  view.setUint16(32, 2, true); // bytes per frame
  view.setUint16(34, 16, true); // bits per sample
  ascii(36, "data");
  view.setUint32(40, pcm.length * 2, true);
  new Int16Array(buffer, 44).set(pcm);
  return new Blob([buffer], { type: "audio/wav" });
}

function bankVoice() {
  // Always the same voice for bank confirmations and answers, so customers learn "this voice = the bank".
  const voices = speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en"));
  return voices.find((v) => /en-(SG|GB)/.test(v.lang)) || voices[0] || null;
}

function chime() {
  try {
    const ctx = new AudioContext();
    [660, 880].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + i * 0.18);
      gain.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + i * 0.18 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + i * 0.18 + 0.3);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + i * 0.18);
      osc.stop(ctx.currentTime + i * 0.18 + 0.32);
    });
  } catch {}
}

let speaking = null;
const TRTC_AUDIO_WAIT_MS = 8000; // no TRTC audio by then: the browser reads the script instead

// The bank voice reads a script the bank rendered. With TRTC it fetches the audio by the
// script's SHA-256, so this page cannot make the voice say anything else; if TRTC is off,
// slow or fails, the browser reads the same script. The island shows the voice while it
// speaks; Face ID takes the island over if an approval starts meanwhile.
function speakBankVoice(script, key) {
  stopSpeaking();
  chime();
  const turn = {};
  speaking = turn;
  const done = () => {
    if (speaking !== turn) return;
    speaking = null;
    clearTimeout(turn.timer);
    turn.audio?.pause();
    if (island.dataset.mode === "voice") setIsland(null);
  };
  if (!island.dataset.mode) setIsland("voice");
  if (key && state?.voice?.speak?.engine === "trtc") {
    const audio = new Audio(`/api/voice/${key}`); // starts fetching during the chime
    turn.audio = audio;
    const fallBack = () => {
      if (speaking !== turn || turn.audio !== audio) return;
      audio.pause();
      audio.removeAttribute("src");
      turn.audio = null;
      speakInBrowser(script, turn, done, 0);
    };
    audio.onended = done;
    audio.onerror = fallBack;
    audio.onplaying = () => {
      // Playing: wait for the end, with a margin in case the player never reports it.
      clearTimeout(turn.timer);
      const seconds = Number.isFinite(audio.duration) ? audio.duration : script.length / 12;
      turn.timer = setTimeout(done, seconds * 1000 + 2000);
    };
    turn.timer = setTimeout(fallBack, TRTC_AUDIO_WAIT_MS);
    setTimeout(() => speaking === turn && audio.play().catch(fallBack), 450);
    return;
  }
  speakInBrowser(script, turn, done, 450);
}

function speakInBrowser(script, turn, done, delay) {
  if (!("speechSynthesis" in window)) return done();
  const u = new SpeechSynthesisUtterance(script);
  u.voice = bankVoice();
  u.rate = 0.95;
  u.onend = u.onerror = done;
  clearTimeout(turn.timer);
  turn.timer = setTimeout(done, delay + 2000 + script.length * 100); // for browsers that never fire "end"
  setTimeout(() => speaking === turn && speechSynthesis.speak(u), delay);
}

function stopSpeaking() {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  if (speaking) {
    clearTimeout(speaking.timer);
    speaking.audio?.pause();
  }
  speaking = null;
}

// ---------------------------------------------------------------------------
// Rogue mode and gateway attacks

async function setRogue() {
  const on = $("#rogue-toggle").checked;
  const { state: s } = await api("/api/rogue", { attack: on ? $("#rogue-attack").value : null });
  renderState(s);
  addMsg("system", on ? "Rogue AI mode is on: the drafting AI is now malicious." : "Rogue AI mode is off.");
}

async function loadAttacks() {
  const { attacks } = await api("/api/attacks");
  const box = $("#attacks");
  box.innerHTML = "";
  for (const a of attacks) {
    const b = el("button", "", a.label);
    b.addEventListener("click", () => runAttack(a));
    box.appendChild(b);
  }
}

async function runAttack(a) {
  const log = $("#attack-log");
  try {
    const { result, state: s } = await api(`/api/attacks/${a.id}`, {});
    const li = el("li", result.blocked ? "blocked" : "breached");
    li.appendChild(el("span", "code", result.blocked ? `${result.status} ${result.code}` : "EXECUTED"));
    li.appendChild(el("span", "", ` ${a.label}`));
    log.prepend(li);
    renderState(s);
  } catch (err) {
    log.prepend(el("li", "breached", `error: ${err.message}`));
  }
  refreshAudit();
}

// ---------------------------------------------------------------------------
// Wiring

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  lastInputWasVoice = false;
  send(input.value);
});
chat.addEventListener("click", (e) => {
  if (e.target.matches(".msg.agent > .suggest") && e.target.closest(".msg") === chat.firstElementChild) {
    lastInputWasVoice = false;
    send(e.target.textContent);
  }
});
$("#passkey-btn").addEventListener("click", registerPasskey);
$("#rogue-toggle").addEventListener("change", setRogue);
$("#rogue-attack").addEventListener("change", () => $("#rogue-toggle").checked && setRogue());
$("#reset").addEventListener("click", async () => {
  const { state: s } = await api("/api/reset", {});
  renderState(s);
  chat.querySelectorAll(".msg:not(:first-child)").forEach((m) => m.remove());
  renderStages([]);
  renderDiff(null);
  $("#attack-log").innerHTML = "";
  refreshAudit();
});

(async function init() {
  tickClock();
  setInterval(tickClock, 10_000);
  setupMic();
  if ("speechSynthesis" in window) speechSynthesis.getVoices();
  renderState(await api("/api/state"));
  renderStages([]);
  await loadAttacks();
  await refreshAudit();
})();
