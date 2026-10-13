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
 * 状态标记。每屏只允许一处持续运动：由页面指定唯一的 live 对象，
 * 其余标记保留颜色与形状但不播放循环。
 */
export function StateMark({ state, label, live = false }: Mark & { live?: boolean }): ReactElement {
  const mark = <StatusMark state={state} label={label} />;
  return live ? mark : <MotionConfig reducedMotion="always">{mark}</MotionConfig>;
}

/** 将宿主秒级时间转换为本机可读时间；缺失值不推断为当前时刻。 */
export function time(value: number | null): string {
  return value === null ? "未提供" : new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}
/** 相对时间只用于扫描；精确时间保留在 title 中。 */
export function relative(value: number | null, now: number): string {
  if (value === null) return "刚刚";
  const seconds = Math.max(0, Math.floor(now / 1000 - value));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return time(value);
}
/** 展示快照中给出的余额，不在浏览器内自行扣减。 */
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
/** 将已命名的宿主事实排成定义列表，保留未知值的明确占位。 */
export function Facts({ items }: { items: [string, Json | undefined][] }): ReactElement {
  return <dl className="facts">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{fact(value)}</dd></div>)}</dl>;
}
/** Inspector 末尾的技术细节，默认折叠；内容原样保留供排障与核对。 */
export function TechDetails({ items, children }: { items: [string, Json | undefined][]; children?: ReactNode }): ReactElement {
  return <details className="tech-details">
    <summary className="pc-focusable">技术细节</summary>
    <Facts items={items} />
    {children}
  </details>;
}
/** 在内容原位说明信息缺失，不用成功或空闲状态替代。 */
export function Unavailable({ children }: { children: ReactNode }): ReactElement { return <p className="muted">{children}</p>; }
/** 静态单色猫头随导航文字取色，不接收状态；首页动态吉祥物仍由 Design 独占。 */
export function BrandMark(): ReactElement {
  return <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">
    <path fill="currentColor" d="M5 15V5l8 5a22 22 0 0 1 6 0l8-5v10c2 3 2 7-1 10-5 5-15 5-20 0-3-3-3-7-1-10Z" />
    <path className="brand-eyes" d="M11 16v4m10-4v4" fill="none" strokeWidth="2.5" strokeLinecap="round" />
  </svg>;
}
/** 页面标题区：headline + 一句说明，右侧可放本页的辅助信息。首页不使用。 */
export function PageHeading({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }): ReactElement {
  return <header className="page-heading">
    <div className="page-title-row"><h1>{title}</h1>{aside}</div>
    <p className="page-intro">{children}</p>
  </header>;
}
/**
 * 区域：只靠标题、距离和共同左轴线成组，不加底色与轮廓。
 * ``more`` 是进入完整页面的入口，以“标题 ›”的形式跟在标题后；``aside`` 放计数等右侧信息。
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
 * 条目行：图标底 + 主标题 + 一行说明 + 尾部状态或操作。
 * 给了 ``onClick`` 时整行是一个按钮；尾部若包含按钮则不要给 ``onClick``，避免按钮嵌套。
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
/** 区域内的空状态：与条目行同一轴线的一句说明，写清楚什么时候会出现内容。 */
export function EmptyNote({ icon = "check", children }: { icon?: IconName; children: ReactNode }): ReactElement {
  return <div className="item-row empty-note"><span className="item-icon" aria-hidden="true"><Icon name={icon} /></span><span className="item-text">{children}</span></div>;
}
/** 整页空状态：图标 + 标题 + 说明 + 可选的去处，留在页面左轴线上。 */
export function EmptyState({ icon = "check", title, children, action }: { icon?: IconName; title: string; children: ReactNode; action?: ReactNode }): ReactElement {
  return <section className="empty-state">
    <span className="item-icon" aria-hidden="true"><Icon name={icon} /></span>
    <h2>{title}</h2>
    <p className="muted">{children}</p>
    {action}
  </section>;
}
