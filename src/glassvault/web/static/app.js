// Glass Vault demo UI. Plain JS, no build step.
// The server owns the conversation and the bank; this page renders what it returns,
// asks the device's passkey (Face ID / Touch ID) to sign, and speaks the bank's script.

const $ = (sel) => document.querySelector(sel);
const chat = $("#chat");
const input = $("#input");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const STAGES = [
  ["injection-scan", "Scam-script scan"],
  ["quarantine", "Untrusted text quarantined"],
  ["draft", "AI draft"],
  ["provenance", "Every detail traced to your words"],
  ["resolve", "Maths done in code"],
  ["validator", "Independent AI re-derivation"],
  ["policy", "Risk and KYC rules"],
  ["challenge", "Bank-rendered confirmation"],
  ["signature", "Your Face ID signature"],
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
  card.querySelector(".listen").addEventListener("click", () => speakBankVoice(o.confirmation.spoken_script));
  card.querySelector(".cancel").addEventListener("click", () => {
    card.remove();
    cancel();
  });

  const wrap = el("div", "msg agent");
  wrap.appendChild(el("div", "", o.message));
  wrap.appendChild(card);
  chat.appendChild(wrap);
  if (lastInputWasVoice) speakBankVoice(o.confirmation.spoken_script);
}

// ---------------------------------------------------------------------------
// Security console

function renderStages(trace, { signature } = {}) {
  const list = $("#stages");
  list.innerHTML = "";
  const byStage = new Map(trace.map((t) => [t.stage, t]));
  if (signature) byStage.set("signature", signature);
  const failedAt = STAGES.findIndex(([key]) => byStage.get(key) && !byStage.get(key).ok);

  STAGES.forEach(([key, label], i) => {
    const t = byStage.get(key);
    let status = t ? (t.ok ? "ok" : "fail") : failedAt !== -1 && i > failedAt ? "skip" : "pending";
    const li = el("li", status);
    li.appendChild(el("span", `dot ${status}`));
    const body = el("div", "stage-body");
    body.appendChild(el("div", "stage-name", label));
    body.appendChild(el("div", "detail", t ? t.detail : status === "skip" ? "not reached" : "waiting"));
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
    li.appendChild(el("span", "kind", e.kind));
    li.appendChild(el("span", "at", e.at.slice(11, 19)));
    li.appendChild(el("span", "hash", e.hash.slice(0, 12)));
    list.appendChild(li);
  }
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
  const mode = $("#llm-mode");
  mode.textContent = s.llm_mode;
  mode.className = `pill ${s.llm_mode.startsWith("offline") ? "" : "ok"}`;
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
  try {
    let body = { nonce: o.challenge.nonce, step_up_code: card.querySelector(".stepup input")?.value || null };
    if (state.passkey.registered) {
      const cred = await navigator.credentials.get({ publicKey: requestOptions(o.webauthn) });
      body.assertion = assertionToJSON(cred);
    } else {
      body.simulated = true;
    }
    const { receipts, state: s } = await api("/api/approve", body);
    card.querySelector(".confirm-actions").remove();
    const done = addMsg("done", "Done. Receipts:");
    for (const r of receipts) done.appendChild(el("div", "receipt", `${r.reference}  ${r.summary}`));
    renderState(s);
    const trace = [...o.trace, { stage: "signature", ok: true, detail: "gateway verified the signature over this exact plan" }];
    renderStages(trace);
  } catch (err) {
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
// Voice: browser speech recognition in, a fixed "bank voice" out.

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;

function setupMic() {
  const mic = $("#mic");
  if (!Recognition) {
    mic.hidden = true;
    return;
  }
  const start = (e) => {
    e.preventDefault();
    recognition = new Recognition();
    recognition.lang = "en-SG";
    recognition.interimResults = true;
    recognition.onresult = (ev) => {
      input.value = Array.from(ev.results).map((r) => r[0].transcript).join("");
    };
    recognition.onend = () => {
      mic.classList.remove("listening");
      if (input.value.trim()) {
        lastInputWasVoice = true;
        send(input.value);
      }
    };
    mic.classList.add("listening");
    recognition.start();
  };
  const stop = () => recognition && recognition.stop();
  mic.addEventListener("pointerdown", start);
  mic.addEventListener("pointerup", stop);
  mic.addEventListener("pointerleave", stop);
}

function bankVoice() {
  // Always the same voice for bank confirmations, so customers learn "this voice = the bank".
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

function speakBankVoice(script) {
  if (!("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  chime();
  const u = new SpeechSynthesisUtterance(script);
  u.voice = bankVoice();
  u.rate = 0.95;
  setTimeout(() => speechSynthesis.speak(u), 450);
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
  setupMic();
  if ("speechSynthesis" in window) speechSynthesis.getVoices();
  renderState(await api("/api/state"));
  renderStages([]);
  await loadAttacks();
  await refreshAudit();
})();
