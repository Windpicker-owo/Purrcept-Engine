/**
 * 系统：运行状况、主体预算、消息积压、插件清单与辅助工具入口。
 * 单列章节以留白分组，每行按标题 + 说明 + 值展开；技术身份收在折叠区。
 * 预算来自会话快照时刻的值，浏览器不自行倒计时或补充额度。
 */
import type { ReactElement } from "react";
import { Button } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import { targetReason } from "../model/controls";
import type { Session, Snapshot } from "../contracts";
import { deviceMark, mark } from "../semantics";
import type { Mark } from "../semantics";
import { deviceName, stageName } from "../model/names";
import type { Route } from "../model/routes";
import { duration, EmptyNote, PageHeading, Section, StateMark, TechDetails, Unavailable } from "../ui/common";

/** 收尾优先于忙碌；缺失会话始终明确表示未知，不推断为空闲。 */
function sessionMark(session: Session | null): Mark {
  if (session === null) return { state: "warning", label: "状态未提供" };
  if (session.closing) return { state: "waiting-external", label: "正在收尾" };
  return session.busy ? { state: "reasoning", label: "运行中" } : { state: "idle", label: "空闲" };
}
function SessionRow({ title, description, session }: { title: string; description: string; session: Session | null }): ReactElement {
  return <div className="setting-row">
    <div><h3>{title}</h3><p className="muted">{description}</p></div>
    <div>
      <StateMark {...sessionMark(session)} />
      {session?.budgets.map((budget, index) => <p key={index}>本轮已用 {budget.used_requests} / {budget.max_requests} 次模型请求，剩余 {duration(budget.remaining_seconds)}</p>)}
    </div>
  </div>;
}

/**
 * 展示宿主、预算、积压与配置工具入口。
 *
 * state 保留最近观察事实，client 接管有稳定身份的开关控制，go 只切换页面。
 * 组件不取得模型或设备资源，不自行计时或乐观改写开关。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function System({ state, client, go }: { state: PanelState; client: PanelClient; go: (route: Route) => void }): ReactElement {
  const s: Snapshot | null = state.snapshot;
  if (s === null) return <><PageHeading title="系统">Engine 的运行状况与辅助工具。</PageHeading><Unavailable>还没有取得本机服务的状态。</Unavailable></>;
  const count = s.pending_inputs.reduce((sum, item) => sum + item.count, 0);
  return <>
    <PageHeading title="系统">Engine 的运行状况与辅助工具。</PageHeading>

    <Section id="system-health" title="运行状况">
      <div className="setting-row"><div><h3>Engine</h3><p className="muted">关闭浏览器不会影响 Engine 和她正在做的事。</p></div><StateMark {...(state.online ? mark(s.phase) : { state: "warning", label: "连接中断" })} /></div>
      <div className="setting-row"><div><h3>数据保存</h3><p className="muted">消息、记忆和行动记录都保存在本机数据目录。</p></div><StateMark {...(s.authority_available ? { state: "completed", label: "正常" } : { state: "failed", label: "无法保存" })} /></div>
      {s.pending_resources.length > 0 ? <div className="setting-row"><div><h3>正在释放的资源</h3></div><p>{s.pending_resources.join("、")}</p></div> : null}
    </Section>

    <Section id="system-subject" title="她的思考">
      {s.proactive_thinking !== null ? <div className="setting-row">
        <div><h3>主动思考</h3><p className="muted">没有新刺激或没有唤醒主脑，任一状态持续 {duration(s.proactive_thinking.idle_seconds)} 就在主脑空闲后唤起一次。被注意机制暂存的消息会一起处理。她也可以继续休息。</p>
          <p className="muted">上床休息时暂停，起床后重新计时。本次运行有效，重启后使用配置默认值。关闭不会撤回已接手的思考。</p>
          {s.proactive_thinking.last_error !== null ? <p className="muted">输入缓存已满，本次空闲刺激未生成；等待下一个周期。</p> : null}
        </div>
        <div><Button variant="secondary" role="switch" aria-label="主动思考" aria-checked={s.proactive_thinking.enabled}
          disabledReason={targetReason(state, "subject", "set_proactive_thinking")}
          onClick={() => { const p = s.proactive_thinking; if (p !== null) void client.submit("subject", "set_proactive_thinking", p.revision, { enabled: !p.enabled }, p.enabled ? "关闭主动思考" : "开启主动思考"); }}>
          {s.proactive_thinking.enabled ? "已开启" : "已关闭"}
        </Button></div>
      </div> : null}
      <SessionRow title="回应" description="处理新消息并决定怎么做。" session={s.foreground} />
      <SessionRow title="反思" description="回顾经历、整理记忆，不影响回应新消息。" session={s.reflection} />
      {s.knowledge !== null ? <div className="setting-row"><div><h3>记忆检索</h3></div><p>{s.knowledge.semantic_enabled === true ? "语义检索已启用" : "仅关键词检索"}{typeof s.knowledge.index_failure === "string" && s.knowledge.index_failure !== "" ? ` · 索引故障：${s.knowledge.index_failure}` : ""}</p></div> : null}
    </Section>

    <Section id="system-inputs" title="待处理消息" aside={<span className="section-count">{count} / {s.pending_limits.count} 条</span>}>
      {s.pending_inputs.length === 0 ? <EmptyNote icon="inbox">没有积压的消息。</EmptyNote> : <div className="flat-list">{s.pending_inputs.map((item) => <div className="flat-row" key={item.stage}><span>{stageName(item.stage)}</span><span>{item.count} 条</span></div>)}</div>}
    </Section>

    <Section id="system-plugins" title="已启用的插件" aside={<span className="section-count">{s.devices.length} 个</span>}>
      {s.devices.length === 0 ? <EmptyNote icon="device">没有启用带状态的插件。</EmptyNote> : <div className="flat-list">{s.devices.map((device) => <button className="row-button pc-focusable" key={device.instance_id} onClick={() => go({ page: "devices", id: device.instance_id })}>
        <span className="row-title">{deviceName(device)}</span><StateMark {...deviceMark(device)} /><span className="muted">查看设备</span>
      </button>)}</div>}
      <p className="section-note">插件在启动时装配。修改配置文件后需要重启 Engine 才会生效。</p>
    </Section>

    <Section id="system-studio" title="声线调音室">
      <p>上传一段声音样本，调整她的声线并保存为预设。调音室是独立工具，不会启动她或打开扬声器。</p>
      <pre>purrcept-voice-studio --profile 预设文件.json --port 8766 --ffmpeg ffmpeg</pre>
      <p className="muted">在终端运行上面的命令，然后用浏览器打开 http://127.0.0.1:8766 。</p>
    </Section>

    <section className="section" aria-label="技术细节">
      <TechDetails items={[["宿主实例", s.instance_id], ["快照版本", s.host_version], ["阶段", s.phase], ["前台代次", s.foreground?.generation], ["反思代次", s.reflection?.generation], ["会话", s.sessions.map((item) => `${item.scope}@${item.version}${item.active_turn ? `:${item.active_turn}` : ""}`).join("，")], ["积压字节", `${s.pending_inputs.reduce((sum, item) => sum + item.logical_bytes, 0)} / ${s.pending_limits.logical_bytes}`], ["记忆索引", s.knowledge]]} />
    </section>
  </>;
}
