/**
 * 产品页面共用的小部件：状态标记、滑动分段筛选签、事实列表、技术细节折叠与时间格式。
 * 视觉与状态映射全部来自 Cel v0.4；这里只组织语义结构与轻量跟手微动效。
 */
import type { ReactElement, ReactNode } from "react";
import { motion, MotionConfig } from "motion/react";
import { Icon, Panel, StatusMark, transitions } from "@purrcept/cel";
import type { IconName, PanelSurface } from "@purrcept/cel";
import type { Json } from "../contracts";
import type { Mark } from "../semantics";

/**
 * 展示同一份 Cel 状态形状与文案。
 *
 * state/label 来自统一语义映射；live 默认 false，每屏只允许一处持续运动。
 */
export function StateMark({ state, label, live = false }: Mark & { live?: boolean }): ReactElement {
  const mark = <StatusMark state={state} label={label} />;
  return live ? mark : <MotionConfig reducedMotion="always">{mark}</MotionConfig>;
}

/**
 * 带共享滑块微动效的无障碍单选胶囊标签，保留原生 radio 键盘方向键切换能力。
 */
export function FilterPill({
  group,
  value,
  label,
  checked,
  category,
  onChange,
}: {
  group: string;
  value: string;
  label: string;
  checked: boolean;
  category?: string;
  onChange: () => void;
}): ReactElement {
  return (
    <label className="filter" data-category={category} data-selected={checked}>
      <input type="radio" name={group} value={value} checked={checked} onChange={onChange} />
      {checked ? (
        <motion.span
          layoutId={`filter-pill-${group}`}
          className="filter-indicator"
          transition={transitions.snap}
          aria-hidden="true"
        />
      ) : null}
      <span className="filter-label">{label}</span>
    </label>
  );
}

/**
 * 展示宿主真实时间。
 */
export function time(value: number | null): string {
  return value === null ? "未提供" : new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}

/**
 * 将已知发生时间转换为便于扫描的相对文案。
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
 */
export function Facts({ items }: { items: [string, Json | undefined][] }): ReactElement {
  return (
    <dl className="facts">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{fact(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * 在详情末尾保留可展开的技术事实。
 */
export function TechDetails({ items, children }: { items: [string, Json | undefined][]; children?: ReactNode }): ReactElement {
  return (
    <details className="tech-details">
      <summary className="pc-focusable">技术细节</summary>
      <Facts items={items} />
      {children}
    </details>
  );
}

/**
 * 在原位说明当前信息缺失。
 */
export function Unavailable({ children }: { children: ReactNode }): ReactElement {
  return <p className="muted">{children}</p>;
}

/**
 * 绘制随导航文字取色的静态品牌猫头。
 */
export function BrandMark(): ReactElement {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <path fill="currentColor" d="M5 15V5l8 5a22 22 0 0 1 6 0l8-5v10c2 3 2 7-1 10-5 5-15 5-20 0-3-3-3-7-1-10Z" />
      <path className="brand-eyes" d="M11 16v4m10-4v4" fill="none" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

/**
 * 组织页面标题、可选眉标、说明和辅助操作。
 */
export function PageHeading({
  title,
  eyebrow,
  tone = "default",
  aside,
  children,
}: {
  title: string;
  eyebrow?: string;
  tone?: "default" | "notebook";
  aside?: ReactNode;
  children: ReactNode;
}): ReactElement {
  return (
    <header className="page-heading" data-tone={tone}>
      {eyebrow ? <span className="pc-eyebrow">{eyebrow}</span> : null}
      <div className="page-title-row">
        <h1>{title}</h1>
        {aside}
      </div>
      <p className="page-intro">{children}</p>
    </header>
  );
}

/**
 * 通过标题、眉标与双生场域材质组织需要实体承载的页面区域。
 */
export function Section({
  id,
  title,
  eyebrow,
  surface = "default",
  more,
  aside,
  children,
}: {
  id: string;
  title: string;
  eyebrow?: string;
  surface?: PanelSurface;
  more?: () => void;
  aside?: ReactNode;
  children: ReactNode;
}): ReactElement {
  return (
    <Panel id={id} title={title} eyebrow={eyebrow} surface={surface} onNavigate={more} actions={aside}>
      {children}
    </Panel>
  );
}

/**
 * 展示对象标题、说明与尾部状态或操作。
 */
export function ItemRow({
  icon,
  title,
  subtitle,
  trailing,
  onClick,
  current = false,
}: {
  icon: IconName;
  title: ReactNode;
  subtitle?: ReactNode;
  trailing?: ReactNode;
  onClick?: () => void;
  current?: boolean;
}): ReactElement {
  const body = (
    <>
      <span className="item-icon" aria-hidden="true">
        <Icon name={icon} />
      </span>
      <span className="item-text">
        <span className="item-title">{title}</span>
        {subtitle !== undefined ? <span className="item-subtitle">{subtitle}</span> : null}
      </span>
      {trailing !== undefined ? <span className="item-trailing">{trailing}</span> : null}
    </>
  );
  return onClick !== undefined ? (
    <button
      type="button"
      className="item-row pc-focusable"
      data-interactive="true"
      aria-current={current ? "true" : undefined}
      onClick={onClick}
    >
      {body}
    </button>
  ) : (
    <div className="item-row">{body}</div>
  );
}

/**
 * 在现有区域中解释何时会出现内容。
 */
export function EmptyNote({ icon = "check", children }: { icon?: IconName; children: ReactNode }): ReactElement {
  return (
    <div className="item-row empty-note">
      <span className="item-icon" aria-hidden="true">
        <Icon name={icon} />
      </span>
      <span className="item-text">{children}</span>
    </div>
  );
}

/**
 * 为整页提供具名空状态和可选去处。
 */
export function EmptyState({
  icon = "check",
  title,
  children,
  action,
}: {
  icon?: IconName;
  title: string;
  children: ReactNode;
  action?: ReactNode;
}): ReactElement {
  return (
    <section className="empty-state">
      <span className="item-icon" aria-hidden="true">
        <Icon name={icon} />
      </span>
      <h2>{title}</h2>
      <p className="muted">{children}</p>
      {action}
    </section>
  );
}
