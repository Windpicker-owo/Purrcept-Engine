/* 草稿与磁盘版本分离；预览单请求串行合并，旧响应不覆盖新滑块值。 */
"use strict";
const $ = id => document.getElementById(id);
let draft, defaults, revision, token, timer, sending = false, pending = false;
let editVersion = 0;
let edits = Promise.resolve();
const groups = [
  ["空闲活动", "眼睛先发现目标，头部和身体随后转向，间或回看观众。", [
    ["idle_enabled", "持续空闲活动"], ["idle_interval_seconds", "平均停留（秒）",1,12,.1],
    ["idle_gaze_scale", "视线转移幅度",0,2,.05], ["idle_posture_scale", "姿态变化幅度",0,2,.05]]],
  ["习惯姿态", "相当于调节面捕的中性姿势与拍摄角度；方向以模型实际绑定为准。", [
    ["head_x","头部左右",-.6,.6,.01],["head_y","抬头 / 低头",-.6,.6,.01],["head_z","头部侧倾",-.6,.6,.01],
    ["body_x","身体左右",-.5,.5,.01],["body_y","身体俯仰",-.5,.5,.01],["body_z","身体侧倾",-.5,.5,.01]]],
  ["目光与眼睑", "默认注视偏移会与头身补偿共同生效，眨眼仍能完全闭合。", [
    ["gaze_x","注视左右偏移",-.5,.5,.01],["gaze_y","注视上下偏移",-.5,.5,.01],["eye_open","自然睁眼程度",.2,1,.01]]],
  ["嘴部形态与动态", "先调开度增益，再调小音量开口曲线。较低曲线值让轻声也有可见口型。", [
    ["mouth_gain","开度增益",0,4,.05],["mouth_gamma","开口曲线",.4,2,.05],
    ["mouth_form","默认嘴型 / 嘴角",-1,1,.02],["mouth_form_gain","嘴型表情幅度",0,3,.05],
    ["mouth_viseme_form_scale","音素展唇 / 圆唇幅度",0,2,.05],
    ["mouth_attack_seconds","张嘴响应（秒）",.02,.3,.01],["mouth_release_seconds","闭嘴响应（秒）",.03,.4,.01],
    ["mouth_gap_hold_seconds","短停顿保持（秒）",0,.2,.01]]]
];
function notice(text) { $("notice").textContent = text; }
async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json","X-Purrcept-Token":token}, body:JSON.stringify(payload)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || "操作失败，请重试。");
  return result;
}
function status(value) { $("connection").textContent = value.connected ? "VTS 已连接" : "VTS 未连接"; }
function editApi(path, payload) {
  // 保存与预览共享顺序，旧预览不能在保存后覆盖当前现场姿态。
  const result = edits.catch(() => {}).then(() => api(path, payload));
  edits = result;
  return result;
}
function draw() {
  $("controls").replaceChildren();
  for (const [title, description, fields] of groups) {
    const section = document.createElement("section");
    const heading = document.createElement("h2"); heading.textContent = title;
    const info = document.createElement("p"); info.textContent = description;
    const container = document.createElement("div"); container.className = "fields";
    for (const [key, name, min, max, step] of fields) {
      const label = document.createElement("label"); label.textContent = name;
      const input = document.createElement("input"); input.id = key;
      const value = document.createElement("span"); value.className = "value";
      if (min === undefined) { input.type="checkbox"; input.checked=draft[key]; }
      else { input.type="range"; input.min=min; input.max=max; input.step=step; input.value=draft[key]; value.textContent=Number(draft[key]).toFixed(2); }
      input.addEventListener("input", () => {
        draft[key] = input.type === "checkbox" ? input.checked : Number(input.value);
        value.textContent = input.type === "checkbox" ? "" : Number(input.value).toFixed(2);
        changed();
      });
      label.append(value,input); container.append(label);
    }
    section.append(heading,info,container); $("controls").append(section);
  }
}
function changed() {
  editVersion++; $("dirty").textContent="有未保存的调整"; pending=true;
  clearTimeout(timer); timer=setTimeout(preview,100);
}
async function preview() {
  if (sending || !pending) return;
  sending=true; pending=false;
  try { const result=await editApi("/api/preview",{preferences:{...draft},revision}); status(result.status); }
  catch(error) { notice(error.message); }
  finally { sending=false; if(pending) void preview(); }
}
async function load() {
  const state = await api("/api/session"); token=state.token; draft=state.preferences;
  defaults=state.defaults; revision=state.revision; status(state.status); draw();
  $("model").textContent="模型："+draft.model_id; $("audio").disabled=!state.sample_available;
  $("dirty").textContent="已载入保存值";
}
$("reload").onclick=async()=>{try { await load(); pending=true; await preview(); notice("已恢复保存值。"); } catch(error){notice(error.message);} };
$("reset").onclick=()=>{draft={...defaults};draw();changed();notice("默认值正在预览，尚未保存。");};
$("save").onclick=async()=>{
  $("save").disabled=true; const version=editVersion;
  try { const result=await editApi("/api/save",{preferences:{...draft},revision}); revision=result.revision;
    if(version===editVersion) $("dirty").textContent="偏好已保存";
    notice("已保存。正式控制器将读取新偏好，原文件已备份。");
  } catch(error){notice(error.message);} finally{$("save").disabled=false;}
};
for(const action of ["connect","disconnect","audio","stop"]){
  $(action).onclick=async()=>{
    $(action).disabled=true;
    try { const result=await api("/api/device/"+action,{});
      if(action==="connect" || action==="disconnect") {status(result);notice(result.connected ? "已连接，调整滑块可实时观察。" : "连接已断开或尚未成功。" );}
      else notice(action==="audio" ? "试听结束："+result.status : "已停止播放。");
    }catch(error){notice(error.message);}finally{$(action).disabled=false;}
  };
}
load().catch(error=>notice(error.message));
