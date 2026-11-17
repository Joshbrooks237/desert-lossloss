const $ = (id) => document.getElementById(id);

const RING = 2 * Math.PI * 48;
let deviceSig = "";
let listSig = "";
let seeking = false;
let leveling = false;
let coverId = 0;
let gesture = null;
let pendingOrder = null;
let swallowClick = false;

if (new URLSearchParams(location.search).has("shell")) {
  document.body.classList.add("shell");
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function khz(rate) {
  const known = {
    44100: "44.1",
    48000: "48",
    88200: "88.2",
    96000: "96",
    176400: "176.4",
    192000: "192",
    352800: "352.8",
    384000: "384",
    705600: "705.6",
    768000: "768",
  };
  const value = Math.round(Number(rate) || 0);
  if (!value) return "—";
  if (known[value]) return known[value];
  const text = (value / 1000).toFixed(1);
  return text.endsWith(".0") ? text.slice(0, -2) : text;
}

function clock(seconds) {
  const whole = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(whole / 60);
  const hours = Math.floor(minutes / 60);
  const rest = String(whole % 60).padStart(2, "0");
  if (hours > 0) return `${hours}:${String(minutes % 60).padStart(2, "0")}:${rest}`;
  return `${minutes}:${rest}`;
}

function codecName(codec) {
  const names = { wavpack: "wv", vorbis: "vorbis", mp3: "mp3", aac: "aac", alac: "alac", flac: "flac" };
  return (names[codec] || codec || "audio").toUpperCase();
}

function layoutName(layout, channels) {
  const text = String(layout || "").toLowerCase();
  if (text.includes("stereo") || channels === 2) return "stereo";
  if (text.includes("mono") || channels === 1) return "mono";
  return layout || (channels ? `${channels}ch` : "");
}

function rateLabel(track) {
  if (!track) return "";
  if (track.grade === "dsd") return "DSD";
  if (!track.sample_rate) return codecName(track.codec);
  const bits = track.bits ? `${track.bits}/` : "";
  return `${codecName(track.codec)} ${bits}${khz(track.sample_rate)}`;
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function paintDevices(data) {
  const devices = data.devices || [];
  const sig = devices.map((device) => `${device.uid}:${Math.round(device.rate)}`).join("|");
  const select = $("device");
  if (sig !== deviceSig) {
    deviceSig = sig;
    select.innerHTML = devices.map((device) => (
      `<option value="${escapeHtml(device.uid)}">${escapeHtml(device.name)}</option>`
    )).join("");
  }
  if (document.activeElement !== select && data.device_uid) select.value = data.device_uid;
  const device = devices.find((item) => item.uid === data.device_uid);
  const now = data.now;
  const matched = device && now && Math.abs(device.rate - now.sample_rate) < 1.5;
  $("clock").textContent = device ? `Clock ${khz(device.rate)} kHz` : "No output";
  $("clock").classList.toggle("hot", Boolean(matched));
}

function paintList(data) {
  if (gesture) return;
  const list = $("setlist");
  const sig = data.queue.map((track) => track.id).join(",");
  if (pendingOrder) {
    if (sig !== pendingOrder) return;
    pendingOrder = null;
  }
  if (!data.queue.length) {
    if (listSig !== "empty") {
      listSig = "empty";
      list.innerHTML = '<li class="empty-list"><span>Heat on the dash. A case with nothing in it.</span>...the fuck, Gary?</li>';
    }
    return;
  }
  if (sig !== listSig) {
    const top = list.scrollTop;
    listSig = sig;
    list.innerHTML = data.queue.map((track, index) => {
      const number = track.track_no ? String(track.track_no).padStart(2, "0") : String(index + 1).padStart(2, "0");
      const sub = [track.artist, track.album].filter(Boolean).join(" · ");
      const numbered = track.track_no ? "meta" : "index";
      return `<li><button class="row" type="button" draggable="false" data-index="${index}" data-id="${track.id}" data-numbered="${numbered}" data-grade="${track.grade}">
        <span class="num">${number}</span>
        <span class="who"><b>${escapeHtml(track.title)}</b><small>${escapeHtml(sub)}</small></span>
        <span class="spec">${escapeHtml(rateLabel(track))}</span>
        <span class="x" data-remove="${index}" role="button" aria-label="Remove">×</span>
      </button></li>`;
    }).join("");
    list.scrollTop = top;
  }
  list.querySelectorAll(".row").forEach((row, index) => {
    row.classList.toggle("current", index === data.index);
  });
}

function paintNow(data) {
  const now = data.now;
  const playing = data.status === "playing";
  document.body.classList.toggle("playing", playing);
  $("play").setAttribute("aria-label", playing ? "Pause" : "Play");
  const kicker = !now ? "Nothing on the spindle" : playing ? "Playing" : data.status === "paused" ? "Paused" : "Cued";
  $("kicker").textContent = kicker;
  $("title").textContent = now ? now.title : "Open a record.";
  $("artist").textContent = now ? now.artist : "";
  $("album").textContent = now ? now.album : "";
  $("drop-hint").hidden = Boolean(now);

  const grade = now ? now.grade : "quiet";
  const labels = { hires: "Hi-res", lossless: "Lossless", lossy: "Lossy", dsd: "DSD", quiet: "Quiet" };
  $("plaque").dataset.grade = grade;
  $("grade").textContent = labels[grade] || "Quiet";
  $("khz").textContent = now && now.sample_rate ? khz(now.sample_rate) : "—";
  $("bits").textContent = now && now.bits ? String(now.bits) : "—";
  const channel = now ? layoutName(now.layout, now.channels) : "";
  const rate = now && now.bitrate
    ? (now.bitrate >= 1000000
      ? `${(now.bitrate / 1000000).toFixed(1)} mbps`
      : `${Math.round(now.bitrate / 1000)} kbps`)
    : "";
  $("codec").textContent = now
    ? [codecName(now.codec), channel, rate].filter(Boolean).join(" · ")
    : "No file";
  $("delivery").textContent = data.delivery || "";
  $("advice").textContent = data.advice || (now ? "" : "Lossless and hi-res files keep what a stream throws away.");

  const stamp = $("stamp");
  if (now && grade !== "quiet") {
    stamp.hidden = false;
    stamp.dataset.grade = grade;
    stamp.textContent = labels[grade];
  } else {
    stamp.hidden = true;
  }

  const cover = $("cover");
  const dune = $("dune");
  if (now && now.has_art) {
    if (coverId !== now.id) {
      coverId = now.id;
      cover.hidden = true;
      dune.hidden = false;
      cover.src = `/art/${now.id}.jpg`;
    }
  } else {
    coverId = 0;
    cover.removeAttribute("src");
    cover.hidden = true;
    dune.hidden = false;
  }

  const duration = now ? now.duration || 0 : 0;
  const position = seeking ? Number($("seek").value) : data.position || 0;
  $("elapsed").textContent = clock(position);
  $("total").textContent = clock(duration);
  const seek = $("seek");
  seek.max = String(duration || 0);
  seek.disabled = !now || !duration;
  if (!seeking) seek.value = String(Math.min(position, duration || position));
  const fraction = duration ? Math.min(100, (Number(seek.value) / duration) * 100) : 0;
  seek.style.setProperty("--p", `${fraction}%`);
  $("ring").style.strokeDashoffset = String(RING * (1 - fraction / 100));

  const scan = $("scan");
  scan.hidden = !data.scanning;
  scan.textContent = data.scan_label || "";

  const problem = data.setup_error || data.error || data.notice || "";
  const error = $("error");
  error.hidden = !problem;
  error.textContent = problem;

  $("match").setAttribute("aria-pressed", data.match_rate ? "true" : "false");
  if (!leveling) {
    const percent = Math.round((data.volume ?? 1) * 100);
    $("volume").value = String(percent);
    $("volume-read").textContent = String(percent);
    $("volume").style.setProperty("--p", `${percent}%`);
  }
}

async function tick() {
  let delay = 800;
  try {
    const data = await api("/api/state");
    paintDevices(data);
    paintList(data);
    paintNow(data);
    if (data.status === "playing") delay = 200;
  } catch (err) {
    $("error").hidden = false;
    $("error").textContent = "The player stopped answering.";
  }
  setTimeout(tick, delay);
}

$("cover").addEventListener("load", () => {
  $("cover").hidden = false;
  $("dune").hidden = true;
});
$("cover").addEventListener("error", () => {
  $("cover").hidden = true;
  $("dune").hidden = false;
});

$("open-files").addEventListener("click", () => api("/api/pick", { kind: "files" }));
$("open-folder").addEventListener("click", () => api("/api/pick", { kind: "folder" }));
$("clear").addEventListener("click", () => api("/api/clear", {}));
$("play").addEventListener("click", () => api("/api/toggle", {}));
$("next").addEventListener("click", () => api("/api/next", {}));
$("prev").addEventListener("click", () => api("/api/prev", {}));
$("match").addEventListener("click", () => {
  const enabled = $("match").getAttribute("aria-pressed") !== "true";
  $("match").setAttribute("aria-pressed", enabled ? "true" : "false");
  api("/api/match", { enabled });
});
$("device").addEventListener("change", () => api("/api/device", { uid: $("device").value }));
$("eq").addEventListener("click", () => {
  const eq = $("eq");
  if (eq.classList.contains("broken")) return;
  eq.classList.add("broken");
  eq.setAttribute("aria-expanded", "true");
  $("eq-line").hidden = false;
});

function renumberRows() {
  $("setlist").querySelectorAll(".row").forEach((row, index) => {
    row.dataset.index = String(index);
    const remove = row.querySelector("[data-remove]");
    if (remove) remove.dataset.remove = String(index);
    if (row.dataset.numbered === "index") {
      const num = row.querySelector(".num");
      if (num) num.textContent = String(index + 1).padStart(2, "0");
    }
  });
}

function placeRow(clientY) {
  const list = $("setlist");
  const items = [...list.children].filter((item) => item !== gesture.li && item.querySelector(".row"));
  let before = null;
  for (const item of items) {
    const box = item.getBoundingClientRect();
    if (clientY < box.top + box.height / 2) {
      before = item;
      break;
    }
  }
  if (before) list.insertBefore(gesture.li, before);
  else list.appendChild(gesture.li);
  renumberRows();
}

function finishGesture(event) {
  if (!gesture || event.pointerId !== gesture.pointer) return;
  const info = gesture;
  gesture = null;
  clearInterval(info.timer);
  $("setlist").classList.remove("is-sorting");
  info.li.classList.remove("lifting");
  if (!info.moved) return;
  swallowClick = true;
  const rows = [...$("setlist").querySelectorAll(".row")];
  const to = rows.indexOf(info.row);
  if (to < 0 || to === info.from) {
    listSig = "";
    return;
  }
  pendingOrder = rows.map((row) => row.dataset.id).join(",");
  api("/api/move", { from: info.from, to }).catch(() => {
    pendingOrder = null;
    listSig = "";
  });
}

$("setlist").addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  if (event.target.closest("[data-remove]")) return;
  const row = event.target.closest(".row");
  if (!row) return;
  const li = row.closest("li");
  try {
    row.setPointerCapture(event.pointerId);
  } catch (err) {}
  gesture = {
    pointer: event.pointerId,
    row,
    li,
    from: Number(row.dataset.index),
    startY: event.clientY,
    moved: false,
    y: event.clientY,
    timer: 0,
  };
});

function armSort() {
  if (!gesture || gesture.moved) return;
  gesture.moved = true;
  gesture.li.classList.add("lifting");
  $("setlist").classList.add("is-sorting");
  gesture.timer = setInterval(() => {
    if (!gesture) return;
    const list = $("setlist");
    const box = list.getBoundingClientRect();
    const edge = 36;
    if (gesture.y < box.top + edge) list.scrollTop -= 14;
    else if (gesture.y > box.bottom - edge) list.scrollTop += 14;
    else return;
    placeRow(gesture.y);
  }, 32);
}

$("setlist").addEventListener("pointermove", (event) => {
  if (!gesture || event.pointerId !== gesture.pointer) return;
  gesture.y = event.clientY;
  if (!gesture.moved && Math.abs(event.clientY - gesture.startY) < 5) return;
  event.preventDefault();
  armSort();
  placeRow(event.clientY);
});

$("setlist").addEventListener("pointerup", finishGesture);
$("setlist").addEventListener("pointercancel", finishGesture);

$("setlist").addEventListener("click", (event) => {
  if (swallowClick) {
    swallowClick = false;
    event.preventDefault();
    return;
  }
  if (pendingOrder) return;
  const remove = event.target.closest("[data-remove]");
  if (remove) {
    event.stopPropagation();
    api("/api/remove", { index: Number(remove.dataset.remove) });
    return;
  }
  const row = event.target.closest("[data-index]");
  if (row) api("/api/play", { index: Number(row.dataset.index) });
});

const seek = $("seek");
seek.addEventListener("pointerdown", () => { seeking = true; });
seek.addEventListener("input", () => {
  $("elapsed").textContent = clock(seek.value);
  const max = Number(seek.max) || 0;
  const fraction = max ? (Number(seek.value) / max) * 100 : 0;
  seek.style.setProperty("--p", `${fraction}%`);
  $("ring").style.strokeDashoffset = String(RING * (1 - fraction / 100));
});
seek.addEventListener("change", () => {
  seeking = false;
  api("/api/seek", { seconds: Number(seek.value) });
});

const volume = $("volume");
let levelGen = 0;
let volumeSending = false;
let volumeDirty = false;
function commitVolume() {
  leveling = true;
  if (volumeSending) {
    volumeDirty = true;
    return;
  }
  volumeSending = true;
  const gen = ++levelGen;
  const value = Number(volume.value) / 100;
  api("/api/volume", { volume: value }).finally(() => {
    volumeSending = false;
    if (volumeDirty) {
      volumeDirty = false;
      commitVolume();
      return;
    }
    if (gen === levelGen) leveling = false;
  });
}
volume.addEventListener("input", () => {
  leveling = true;
  $("volume-read").textContent = volume.value;
  volume.style.setProperty("--p", `${volume.value}%`);
  commitVolume();
});

window.addEventListener("keydown", (event) => {
  const tag = event.target.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
  if (event.code === "Space") {
    event.preventDefault();
    api("/api/toggle", {});
  } else if (event.code === "ArrowRight") {
    api("/api/seek", { seconds: Number(seek.value) + (event.shiftKey ? 30 : 5) });
  } else if (event.code === "ArrowLeft") {
    api("/api/seek", { seconds: Number(seek.value) - (event.shiftKey ? 30 : 5) });
  } else if (event.code === "ArrowUp" || event.code === "ArrowDown") {
    event.preventDefault();
    const step = event.code === "ArrowUp" ? 5 : -5;
    const next = Math.min(100, Math.max(0, Number(volume.value) + step));
    volume.value = String(next);
    $("volume-read").textContent = volume.value;
    volume.style.setProperty("--p", `${volume.value}%`);
    commitVolume();
  }
});

window.losslossDrop = (paths) => api("/api/library", { paths });

document.addEventListener("mousedown", (event) => {
  if (event.button !== 0) return;
  const drag = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.drag;
  if (!drag) return;
  if (event.target.closest("button, a, input, select, textarea, label, .row")) return;
  event.preventDefault();
  drag.postMessage(null);
});

tick();
