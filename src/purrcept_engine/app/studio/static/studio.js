/* 独立调音页拥有一个播放器与一份草稿。服务拥有样本和预设版本。
 * 异步处理携带 clip 与草稿代次，不覆盖更新后的素材或冒充新参数的结果。
 * 页面不打开设备，也不持有模型服务凭据。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const fields = [
  ["pitch_semitones", "音高 / 半音", -3, 3, 0.1],
  ["highpass_hz", "高通 / Hz", 0, 200, 1],
  ["low_db", "低频 / dB", -9, 9, 0.1],
  ["mid_db", "中频 / dB", -9, 9, 0.1],
  ["mid_hz", "中频中心 / Hz", 300, 5000, 10],
  ["mid_q", "中频 Q", 0.3, 3, 0.1],
  ["high_db", "高频 / dB", -9, 9, 0.1],
  ["gain_db", "输出增益 / dB", -18, 6, 0.1],
];
const flat = {
  version: 1, enabled: false, pitch_semitones: 0, highpass_hz: 0,
  low_db: 0, mid_db: 0, mid_hz: 1800, mid_q: 0.8,
  high_db: 0, gain_db: 0, limiter: true,
};
let token = "", revision = "", clip = "", original = "", processed = "";
let busy = false, timer = null, selection = "original", draftVersion = 0;
const player = $("player");

// 数值范围来自 VoiceProfile 契约；浏览器限制辅助输入，服务再次校验。
for (const [name, title, min, max, step] of fields) {
  const label = document.createElement("label");
  label.className = "parameter";
  const text = document.createElement("span");
  text.textContent = title;
  const number = document.createElement("input");
  number.type = "number";
  number.id = name;
  const range = document.createElement("input");
  range.type = "range";
  range.id = name + "-range";
  range.setAttribute("aria-label", title);
  for (const input of [number, range]) {
    input.min = min;
    input.max = max;
    input.step = step;
  }
  number.addEventListener("input", () => {
    range.value = number.value;
    changed();
  });
  range.addEventListener("input", () => {
    number.value = range.value;
    changed();
  });
  label.append(text, number, range);
  $("sliders").append(label);
}

function status(id, message, state = "idle") {
  $(id).textContent = message;
  $(id).dataset.state = state;
}

function enable(id, allowed, reason = "") {
  $(id).disabled = !allowed;
  $(id).title = allowed ? "" : reason;
}

function draft() {
  const value = {version: 1, enabled: $("enabled").checked, limiter: $("limiter").checked};
  for (const [name] of fields) value[name] = Number($(name).value);
  return value;
}

function fill(profile) {
  if (profile === null || typeof profile !== "object" || Array.isArray(profile) ||
      profile.version !== 1 || Object.keys(flat).some((name) => !(name in profile)) ||
      Object.keys(profile).some((name) => !(name in flat))) {
    throw new Error("预设版本或字段无效，请使用当前版本预设。");
  }
  for (const [name, , min, max] of fields) {
    if (!Number.isFinite(profile[name]) || profile[name] < min || profile[name] > max) {
      throw new Error("预设参数超出允许范围，当前草稿未改动。");
    }
  }
  if (typeof profile.enabled !== "boolean" || typeof profile.limiter !== "boolean") {
    throw new Error("预设开关字段无效，当前草稿未改动。");
  }

  // 全部验证后才替换控件，坏预设不能留下半份修改过的草稿。
  for (const [name] of fields) $(name).value = $(name + "-range").value = profile[name];
  $("enabled").checked = profile.enabled;
  $("limiter").checked = profile.limiter;
  draftVersion += 1;
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options, headers: {...options.headers, "X-Purrcept-Token": token},
  });
  if (!response.ok) {
    if (!response.headers.get("content-type")?.includes("application/json")) {
      throw new Error("本机处理失败，结果未确认，请查看服务日志并重试。");
    }
    const error = await response.json();
    throw new Error(error.message || "操作未完成，请重新载入页面查看当前状态。");
  }
  return response;
}

function changed() {
  draftVersion += 1;
  status("save-message", "草稿已修改，保存后用于下一段语音。", "waiting-user");
  if (timer) clearTimeout(timer);
  // 一个可替换定时器合并拖动；处理中的修改由 render 收尾后采用。
  timer = setTimeout(() => { if (clip && !busy) render(); }, 400);
}

function release(url) {
  if (url.startsWith("blob:")) URL.revokeObjectURL(url);
}

function choose(which) {
  const url = which === "original" ? original : processed;
  if (!url) return;
  const position = player.currentTime, playing = !player.paused;
  player.pause();
  selection = which;
  player.src = url;
  player.addEventListener("loadedmetadata", () => {
    player.currentTime = Math.min(position, Number.isFinite(player.duration) ? player.duration : position);
    if (playing) player.play().catch(() => {
      status("preview-status", "浏览器未开始播放，请使用播放按钮。", "waiting-user");
    });
  }, {once: true});
  $("original").setAttribute("aria-pressed", String(which === "original"));
  $("processed").setAttribute("aria-pressed", String(which === "processed"));
}

async function waveform(blob) {
  // 只解码服务返回的有界 WAV 画波形；分析上下文立即释放，不自动播放。
  const context = new AudioContext();
  try {
    const buffer = await context.decodeAudioData(await blob.arrayBuffer());
    const data = buffer.getChannelData(0), bars = document.createDocumentFragment();
    for (let index = 0; index < 80; index++) {
      const start = Math.floor(index * data.length / 80);
      const end = Math.floor((index + 1) * data.length / 80);
      let peak = 0;
      for (let sample = start; sample < end; sample++) peak = Math.max(peak, Math.abs(data[sample]));
      const bar = document.createElement("i");
      bar.style.height = Math.max(2, peak * 100) + "%";
      bars.append(bar);
    }
    $("waveform").replaceChildren(bars);
  } finally {
    await context.close();
  }
}

async function upload(file) {
  if (!file) return;
  if (busy) {
    status("preview-status", "当前试听尚未完成，请稍后重新上传。", "waiting-user");
    return;
  }
  busy = true;
  let adopted = false;
  status("preview-status", "正在读取本机素材。", "active");
  try {
    const value = await (await request("/api/source", {method: "POST", body: file})).json();
    clip = value.clip_id;
    const blob = await (await request("/api/source/" + encodeURIComponent(clip))).blob();

    // 服务确认替换后收回旧播放源，单播放器不会同时保留 A/B 两路播放。
    player.pause();
    release(original);
    release(processed);
    processed = "";
    original = URL.createObjectURL(blob);
    player.src = original;
    selection = "original";
    $("original").setAttribute("aria-pressed", "true");
    $("processed").setAttribute("aria-pressed", "false");
    $("filename").textContent = file.name;
    $("duration").textContent = value.duration.toFixed(2) + " 秒";
    enable("original", true);
    enable("render", true);
    enable("processed", false, "请先生成处理版");
    enable("download", false, "请先生成处理版");
    adopted = true;
    await waveform(blob);
    status("preview-status", "原声已载入，可以调整参数并试听。", "completed");
  } catch (error) {
    status("preview-status", error.message, "failed");
  } finally {
    busy = false;
  }
  if (adopted) await render();
}

async function render() {
  if (!clip || busy) return;
  busy = true;
  const identity = clip, version = draftVersion;
  enable("render", false, "正在生成处理版");
  status("preview-status", "正在使用当前草稿生成试听。", "active");
  try {
    const response = await request("/api/render", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({clip_id: identity, profile: draft()}),
    });
    const blob = await response.blob();
    // 旧草稿不显示为新参数结果；最多在收尾后处理最新一份草稿。
    if (identity !== clip || version !== draftVersion) return;
    const old = processed;
    processed = URL.createObjectURL(blob);
    if (selection === "processed") choose("processed");
    release(old);
    enable("processed", true);
    enable("download", true);
    status("preview-status", "处理版已更新，可切换 A / B 对比。", "completed");
  } catch (error) {
    status("preview-status", error.message, "failed");
  } finally {
    busy = false;
    enable("render", !!clip, "请先上传素材");
    if (identity === clip && version !== draftVersion) {
      if (timer) clearTimeout(timer);
      timer = setTimeout(render, 0);
    }
  }
}

async function reload() {
  try {
    const value = await (await request("/api/profile")).json();
    fill(value.profile);
    revision = value.revision;
    enable("save", true);
    status("save-message", "已载入保存的预设。", "completed");
    if (clip) await render();
  } catch (error) {
    status("save-message", error.message, "failed");
  }
}

function download(url, name) {
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
}

$("audio-file").addEventListener("change", (event) => upload(event.target.files[0]));
$("drop").addEventListener("dragover", (event) => event.preventDefault());
$("drop").addEventListener("drop", (event) => {
  event.preventDefault();
  upload(event.dataTransfer.files[0]);
});
$("original").addEventListener("click", () => choose("original"));
$("processed").addEventListener("click", () => choose("processed"));
$("loop").addEventListener("change", () => { player.loop = $("loop").checked; });
player.loop = true;
$("enabled").addEventListener("change", changed);
$("limiter").addEventListener("change", changed);
$("render").addEventListener("click", render);
$("reload").addEventListener("click", reload);
$("reset").addEventListener("click", () => { fill(flat); changed(); });
$("download").addEventListener("click", () => download(processed, "purrcept-processed.wav"));
$("export-profile").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(draft(), null, 2)], {type: "application/json"}));
  download(url, "purrcept-voice-profile.json");
  setTimeout(() => release(url), 1000);
});
$("import-profile").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 16384) throw new Error("预设超过 16 KiB，请选择有效预设。");
    fill(JSON.parse(await file.text()));
    changed();
  } catch (error) {
    status("save-message", error.message, "failed");
  }
});
$("save").addEventListener("click", async () => {
  enable("save", false, "正在保存预设");
  const version = draftVersion;
  try {
    const value = await (await request("/api/profile", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({revision, profile: draft()}),
    })).json();
    revision = value.revision;
    status("save-message", version === draftVersion ?
      "预设已保存，下一段现场语音采用新设置。" : "上一份草稿已保存，当前修改尚未保存。",
      version === draftVersion ? "completed" : "waiting-user");
  } catch (error) {
    status("save-message", error.message, "failed");
  } finally {
    enable("save", true);
  }
});

(async () => {
  try {
    const value = await (await request("/api/session")).json();
    token = value.token;
    await reload();
    if (value.clip_id) {
      clip = value.clip_id;
      original = "/api/source/" + encodeURIComponent(clip);
      player.src = original;
      $("duration").textContent = value.duration.toFixed(2) + " 秒";
      enable("original", true);
      enable("render", true);
      status("preview-status", "当前服务已有素材，可继续试听。", "completed");
    }
  } catch (error) {
    status("save-message", error.message, "failed");
  }
})();