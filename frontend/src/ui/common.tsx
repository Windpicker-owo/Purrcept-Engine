/**
 * 产品页面共用的小部件：状态标记、事实列表、技术细节折叠与时间格式。
 * 视觉与状态映射全部来自 Cel；这里只组织结构，不定义第二套状态表达。
 */
import type { ReactElement, ReactNode } from "react";
import { MotionConfig } from "motion/react";
import { Icon, StatusMark } from "@purrcept/cel";
import type { IconName } from "@purrcept/cel";
import type { Json } from "../contracts";
import type { Mark } from "../semantics";

/**
 * 展示同一份 Cel 状态形状与文案。
 *
 * state/label 来自统一语义映射；live 默认 false，每屏只允许一处持续运动。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function StateMark({ state, label, live = false }: Mark & { live?: boolean }): ReactElement {
  const mark = <StatusMark state={state} label={label} />;
  return live ? mark : <MotionConfig reducedMotion="always">{mark}</MotionConfig>;
}

/**
 * 展示宿主真实时间。
 *
 * @param value 宿主使用的 Unix 秒级时间；null 表示未提供。
 * @returns 按本机区域显示的时间，或明确的缺失占位。
 */
export function time(value: number | null): string {
  return value === null ? "未提供" : new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}
/**
 * 将已知发生时间转换为便于扫描的相对文案。
 *
 * @param value 宿主实际保存的 Unix 秒级时间，null 表示未知。
 * @param now 调用者传入的当前毫秒时间，不与 value 的单位混用。
 * @returns 用于扫描的相对时间；超过一天时回到精确时间显示。
 */
export function relative(value: number | null, now: number): string {
  if (value === null) return "时间未知";
  const seconds = Math.max(0, Math.floor(now / 1000 - value));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return time(value);
}
/**
 * 将宿主提供的预算秒数格式化为可读时长。
 *
 * @param value 宿主快照中的剩余秒数，浏览器不自行扣减。
 * @returns 按小时、分钟与秒呈现的预算余额。
 */
export function duration(value: number): string {
  const minutes = Math.floor(value / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : `${minutes} 分 ${Math.floor(value % 60)} 秒`;
}
function fact(value: Json | undefined): string {
  if (value === undefined || value === null) return "未提供";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value).slice(0, 512);
  return String(value);
}
/**
 * 将具名业务事实排为可扫描列表。
 *
 * items 是标签与 JSON 值的顺序对；缺失值显示占位，对象仅展示有限预览。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function Facts({ items }: { items: [string, Json | undefined][] }): ReactElement {
  return <dl className="facts">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{fact(value)}</dd></div>)}</dl>;
}
/**
 * 在详情末尾保留可展开的技术事实。
 *
 * items 原样交给 Facts；children 可补充核对说明，默认折叠不隐藏原始事实。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function TechDetails({ items, children }: { items: [string, Json | undefined][]; children?: ReactNode }): ReactElement {
  return <details className="tech-details">
    <summary className="pc-focusable">技术细节</summary>
    <Facts items={items} />
    {children}
  </details>;
}
/**
 * 在原位说明当前信息缺失。
 *
 * children 应写明缺少什么，不能用空闲或成功文案替代未知状态。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function Unavailable({ children }: { children: ReactNode }): ReactElement { return <p className="muted">{children}</p>; }
/**
 * 绘制随导航文字取色的静态品牌猫头。
 *
 * 图标不接受主体状态；持续动态吉祥物由页面和 Design 组件持有。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function BrandMark(): ReactElement {
  return <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">
    <path fill="currentColor" d="M5 15V5l8 5a22 22 0 0 1 6 0l8-5v10c2 3 2 7-1 10-5 5-15 5-20 0-3-3-3-7-1-10Z" />
    <path className="brand-eyes" d="M11 16v4m10-4v4" fill="none" strokeWidth="2.5" strokeLinecap="round" />
  </svg>;
}
/**
 * 组织页面标题、说明和可选辅助信息。
 *
 * title 是主标题，aside 放辅助信息，children 是一句页面说明；首页不使用。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function PageHeading({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }): ReactElement {
  return <header className="page-heading">
    <div className="page-title-row"><h1>{title}</h1>{aside}</div>
    <p className="page-intro">{children}</p>
  </header>;
}
/**
 * 通过标题与阅读轴线组织一个页面区域。
 *
 * id 关联无障碍标题；more 只承担导航，aside 放计数，children 是区域内容。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function Section({ id, title, more, aside, children }: { id: string; title: string; more?: () => void; aside?: ReactNode; children: ReactNode }): ReactElement {
  return <section className="section" aria-labelledby={id}>
    <div className="section-head">
      {more !== undefined
        ? <h2 id={id}><button className="section-link pc-focusable" onClick={more}>{title}<Icon name="chevron" /></button></h2>
        : <h2 id={id}>{title}</h2>}
      {aside}
    </div>
    {children}
  </section>;
}
/**
 * 展示对象标题、说明与尾部状态或操作。
 *
 * onClick 存在时整行是按钮；trailing 内含按钮时应省略 onClick，避免嵌套交互。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function ItemRow({ icon, title, subtitle, trailing, onClick, current = false }: {
  icon: IconName; title: ReactNode; subtitle?: ReactNode; trailing?: ReactNode; onClick?: () => void; current?: boolean;
}): ReactElement {
  const body = <>
    <span className="item-icon" aria-hidden="true"><Icon name={icon} /></span>
    <span className="item-text"><span className="item-title">{title}</span>{subtitle !== undefined ? <span className="item-subtitle">{subtitle}</span> : null}</span>
    {trailing !== undefined ? <span className="item-trailing">{trailing}</span> : null}
  </>;
  return onClick !== undefined
    ? <button className="item-row pc-focusable" data-interactive="true" aria-current={current ? "true" : undefined} onClick={onClick}>{body}</button>
    : <div className="item-row">{body}</div>;
}
/**
 * 在现有区域中解释何时会出现内容。
 *
 * icon 默认 check，children 说明当前为空的实际条件。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function EmptyNote({ icon = "check", children }: { icon?: IconName; children: ReactNode }): ReactElement {
  return <div className="item-row empty-note"><span className="item-icon" aria-hidden="true"><Icon name={icon} /></span><span className="item-text">{children}</span></div>;
}
/**
 * 为整页提供具名空状态和可选去处。
 *
 * title/children 解释缺少内容的原因；action 是明确导航或操作入口。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function EmptyState({ icon = "check", title, children, action }: { icon?: IconName; title: string; children: ReactNode; action?: ReactNode }): ReactElement {
  return <section className="empty-state">
    <span className="item-icon" aria-hidden="true"><Icon name={icon} /></span>
    <h2>{title}</h2>
    <p className="muted">{children}</p>
    {action}
  </section>;
}
