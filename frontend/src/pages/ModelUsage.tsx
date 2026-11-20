/**
 * API 统计页（Purrcept Cel 清透学园清爽数据版）：
 * 以克制、干净的数据可读性为核心，去除水印、网点、胶带与高饱和拼色干扰，
 * 仅保留统一清透蓝灰主调：顶部时间窗切换 → 核心指标总览条 → 单色系趋势图 → 对齐明细表（按模型 / 按模块 / 最近记录）。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { Button, Feedback } from "@purrcept/cel";
import type { PanelClient } from "../client";
import type {
  ModelPerformanceEntry,
  ModelRecentAttempt,
  ModelScopeEntry,
  ModelUsage,
  ModelUsageBucket,
  ModelUsagePeriod,
} from "../contracts";
import type { Route } from "../model/routes";
import { EmptyNote, FilterPill, PageHeading, relative, StateMark, time } from "../ui/common";

type ChartMode = "tokens" | "latency" | "calls";
type DetailTab = "models" | "scopes" | "recent";

const PERIODS: readonly { value: ModelUsagePeriod; label: string }[] = [
  { value: "24h", label: "24 小时" },
  { value: "7d", label: "7 天" },
  { value: "30d", label: "30 天" },
  { value: "all", label: "全部" },
];

const CHART_MODES: readonly { value: ChartMode; label: string }[] = [
  { value: "tokens", label: "Token 用量" },
  { value: "latency", label: "响应延迟" },
  { value: "calls", label: "调用次数" },
];

const SCOPE_LABELS: Readonly<Record<string, string>> = {
  foreground: "主脑前台对话",
  attention: "注意门控判断",
  reflection: "记忆回顾整理",
  behavior_review: "行为复盘评审",
  behavior_analysis: "行为归纳分析",
  proactive: "主动空闲思考",
  other: "其他系统调用",
};

const numberFormat = new Intl.NumberFormat("zh-CN");
const compactFormat = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 });
const percentFormat = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });

function count(value: number): string {
  return numberFormat.format(value);
}

function compactCount(value: number): string {
  return value < 10_000 ? numberFormat.format(value) : compactFormat.format(value);
}

function formatLatency(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 10_000) return `${(ms / 1000).toFixed(2)} s`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function formatRate(rate: number | null): string {
  return rate === null ? "—" : percentFormat.format(rate);
}

function formatThroughput(tps: number | null): string {
  return tps === null ? "—" : `${tps.toFixed(1)} tok/s`;
}

function formatBucketTick(ts: number, bucketSeconds: number): string {
  const d = new Date(ts * 1000);
  const pad = (n: number): string => String(n).padStart(2, "0");
  if (bucketSeconds <= 3600) {
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  if (bucketSeconds < 86400) {
    return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function formatBucketRange(bucket: ModelUsageBucket, bucketSeconds: number): string {
  const start = new Date(bucket.start_at * 1000);
  const end = new Date(bucket.end_at * 1000);
  const pad = (n: number): string => String(n).padStart(2, "0");
  const sDate = `${start.getMonth() + 1}/${start.getDate()}`;
  if (bucketSeconds < 86400) {
    return `${sDate} ${pad(start.getHours())}:${pad(start.getMinutes())}–${pad(end.getHours())}:${pad(end.getMinutes())}`;
  }
  const eDate = `${end.getMonth() + 1}/${end.getDate()}`;
  return bucketSeconds === 86400 ? sDate : `${sDate}–${eDate}`;
}

function scopeLabel(scope: string): string {
  return SCOPE_LABELS[scope] ?? scope;
}

function outcomeState(outcome: string): { state: "completed" | "failed" | "warning" | "running"; label: string } {
  switch (outcome) {
    case "succeeded":
      return { state: "completed", label: "成功" };
    case "running":
      return { state: "running", label: "进行中" };
    case "timed_out":
      return { state: "warning", label: "超时" };
    case "cancelled":
    case "interrupted":
      return { state: "warning", label: outcome === "cancelled" ? "已取消" : "中断" };
    default:
      return { state: "failed", label: "失败" };
  }
}

/**
 * 克制清爽的单色系时序趋势图：仅用深浅两阶清透蓝（及必要的异常红点缀）呈现走势。
 */
function UsageTrendChart({
  series,
  bucketSeconds,
  mode,
  activeIndex,
  onSelectIndex,
}: {
  series: readonly ModelUsageBucket[];
  bucketSeconds: number;
  mode: ChartMode;
  activeIndex: number | null;
  onSelectIndex: (idx: number | null) => void;
}): ReactElement {
  const width = 780;
  const height = 204;
  const padLeft = 50;
  const padRight = 28;
  const padTop = 14;
  const padBottom = 28;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;
  const baseY = padTop + plotH;

  const maxVal = useMemo(() => {
    let m = 0;
    for (const b of series) {
      const v =
        mode === "tokens"
          ? b.total_tokens
          : mode === "latency"
            ? Math.max(b.p95_latency_ms ?? 0, b.avg_latency_ms ?? 0)
            : b.attempts;
      if (v > m) m = v;
    }
    if (m <= 0) return mode === "latency" ? 1000 : mode === "calls" ? 4 : 1000;
    return m * 1.12;
  }, [series, mode]);

  const countBuckets = Math.max(series.length, 1);
  const slotW = plotW / countBuckets;
  const barW = Math.max(6, Math.min(22, slotW * 0.58));
  const tickStep = Math.max(1, Math.ceil(series.length / 6));

  const yTicks = [0, 0.5, 1].map((ratio) => {
    const val = maxVal * ratio;
    const y = baseY - plotH * ratio;
    const label =
      ratio === 0
        ? "0"
        : mode === "latency"
          ? val >= 1000
            ? `${(val / 1000).toFixed(1)}s`
            : `${Math.round(val)}ms`
          : mode === "calls"
            ? String(Math.max(1, Math.round(val)))
            : compactCount(Math.round(val));
    return { ratio, y, label };
  });

  const latencyGeometry = useMemo(() => {
    if (mode !== "latency" || series.length === 0) return null;
    const avgPts: { x: number; y: number; idx: number }[] = [];
    const p95Pts: { x: number; y: number; idx: number }[] = [];

    series.forEach((b, idx) => {
      const cx = padLeft + idx * slotW + slotW / 2;
      if (b.avg_latency_ms !== null) {
        avgPts.push({ x: cx, y: baseY - Math.min(1, b.avg_latency_ms / maxVal) * plotH, idx });
      }
      if (b.p95_latency_ms !== null) {
        p95Pts.push({ x: cx, y: baseY - Math.min(1, b.p95_latency_ms / maxVal) * plotH, idx });
      }
    });

    const avgPath = avgPts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
    const p95Path = p95Pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
    const areaPath =
      avgPts.length >= 2
        ? `${avgPath} L${avgPts[avgPts.length - 1]!.x.toFixed(1)},${baseY.toFixed(1)} L${avgPts[0]!.x.toFixed(1)},${baseY.toFixed(1)} Z`
        : "";
    return { avgPts, p95Pts, avgPath, p95Path, areaPath };
  }, [mode, series, slotW, baseY, plotH, maxVal]);

  return (
    <div className="usage-chart-frame" onMouseLeave={() => onSelectIndex(null)}>
      <svg
        className="usage-svg"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={mode === "tokens" ? "Token 用量趋势" : mode === "latency" ? "响应延迟趋势" : "调用次数趋势"}
      >
        {yTicks.map((t) => (
          <g key={t.ratio}>
            <line
              x1={padLeft}
              x2={width - padRight}
              y1={t.y}
              y2={t.y}
              className={t.ratio === 0 ? "usage-axis-baseline" : "usage-axis-grid"}
            />
            <text x={padLeft - 8} y={t.y + 4} textAnchor="end" className="usage-axis-label">
              {t.label}
            </text>
          </g>
        ))}

        {activeIndex !== null && series[activeIndex] !== undefined ? (
          <rect
            x={padLeft + activeIndex * slotW + 2}
            y={padTop - 2}
            width={Math.max(2, slotW - 4)}
            height={plotH + 2}
            rx={4}
            className="usage-bucket-highlight"
          />
        ) : null}

        {/* Token 用量：浅蓝（输入）+ 主色蓝（输出），无重黑描边 */}
        {mode === "tokens" &&
          series.map((b, idx) => {
            const bx = padLeft + idx * slotW + (slotW - barW) / 2;
            if (b.total_tokens <= 0) {
              return <rect key={b.start_at} x={bx} y={baseY - 1.5} width={barW} height={1.5} className="usage-bar-idle" />;
            }
            const totalH = Math.max(3, Math.min(plotH, (b.total_tokens / maxVal) * plotH));
            const outH = Math.min(totalH, (b.output_tokens / b.total_tokens) * totalH);
            const inH = Math.max(0, totalH - outH);
            const yTop = baseY - totalH;
            return (
              <g key={b.start_at}>
                {inH > 0.5 ? (
                  <rect x={bx} y={yTop + outH} width={barW} height={inH} rx={2} className="usage-bar-soft" />
                ) : null}
                {outH > 0.5 ? (
                  <rect x={bx} y={yTop} width={barW} height={outH} rx={2} className="usage-bar-primary" />
                ) : null}
              </g>
            );
          })}

        {/* 响应延迟：主色蓝实线（均值）+ 浅灰虚线（P95） */}
        {mode === "latency" && latencyGeometry !== null ? (
          <g>
            {latencyGeometry.areaPath ? <path d={latencyGeometry.areaPath} className="usage-lat-area" /> : null}
            {latencyGeometry.p95Path ? <path d={latencyGeometry.p95Path} className="usage-lat-p95-line" /> : null}
            {latencyGeometry.avgPath ? <path d={latencyGeometry.avgPath} className="usage-lat-avg-line" /> : null}
            {latencyGeometry.avgPts.map((pt) => (
              <circle
                key={`avg-${pt.idx}`}
                cx={pt.x}
                cy={pt.y}
                r={activeIndex === pt.idx ? 4.5 : 3}
                className="usage-lat-avg-dot"
              />
            ))}
          </g>
        ) : null}

        {/* 调用次数：成功主色蓝 + 异常红顶标 */}
        {mode === "calls" &&
          series.map((b, idx) => {
            const bx = padLeft + idx * slotW + (slotW - barW) / 2;
            if (b.attempts <= 0) {
              return <rect key={b.start_at} x={bx} y={baseY - 1.5} width={barW} height={1.5} className="usage-bar-idle" />;
            }
            const totalH = Math.max(3, Math.min(plotH, (b.attempts / maxVal) * plotH));
            const failH = (b.failed_attempts / b.attempts) * totalH;
            const okH = Math.max(0, totalH - failH);
            const yTop = baseY - totalH;
            return (
              <g key={b.start_at}>
                {okH > 0.5 ? (
                  <rect x={bx} y={yTop + failH} width={barW} height={okH} rx={2} className="usage-bar-primary" />
                ) : null}
                {failH > 0.5 ? (
                  <rect x={bx} y={yTop} width={barW} height={failH} rx={2} className="usage-bar-fail" />
                ) : null}
              </g>
            );
          })}

        {/* X 轴时间刻度 */}
        {series.map((b, idx) => {
          const lastIdx = series.length - 1;
          const isEdgeEnd = idx === lastIdx;
          const isRegular = idx % tickStep === 0;
          if (!isRegular && !isEdgeEnd) return null;
          if (isRegular && !isEdgeEnd && lastIdx - idx < Math.ceil(tickStep * 0.75) && series.length > 4) {
            return null;
          }
          const cx = isEdgeEnd ? width - padRight : padLeft + idx * slotW + slotW / 2;
          const anchor = isEdgeEnd ? "end" : idx === 0 ? "start" : "middle";
          const xPos = idx === 0 && !isEdgeEnd ? padLeft : cx;
          return (
            <text key={`x-${b.start_at}`} x={xPos} y={height - 7} textAnchor={anchor} className="usage-axis-label">
              {formatBucketTick(b.start_at, bucketSeconds)}
            </text>
          );
        })}

        {/* 悬停与键盘检视热区 */}
        {series.map((b, idx) => (
          <rect
            key={`hit-${b.start_at}`}
            x={padLeft + idx * slotW}
            y={padTop - 4}
            width={slotW}
            height={plotH + 8}
            className="usage-hit-col"
            tabIndex={0}
            role="button"
            aria-label={`${formatBucketRange(b, bucketSeconds)}：${count(b.total_tokens)} token，${count(b.attempts)} 次调用，平均延迟 ${formatLatency(b.avg_latency_ms)}`}
            onMouseEnter={() => onSelectIndex(idx)}
            onFocus={() => onSelectIndex(idx)}
            onBlur={() => onSelectIndex(null)}
          />
        ))}
      </svg>
    </div>
  );
}

function ModelsTable({
  models,
  totalTokens,
}: {
  models: readonly ModelPerformanceEntry[];
  totalTokens: number;
}): ReactElement {
  const denom = Math.max(totalTokens, 1);
  return (
    <div className="usage-table-wrap">
      <table className="usage-data-table" aria-label="按模型统计明细">
        <thead>
          <tr>
            <th scope="col">模型</th>
            <th scope="col">调用 / 成功率</th>
            <th scope="col">输入 / 输出</th>
            <th scope="col">总 Token</th>
            <th scope="col">平均 / P95 延迟</th>
            <th scope="col">速率</th>
            <th scope="col">缓存命中</th>
          </tr>
        </thead>
        <tbody>
          {models.map((item) => {
            const share = totalTokens > 0 ? (item.total_tokens / denom) * 100 : 0;
            return (
              <tr key={item.model}>
                <th scope="row" className="usage-col-name">
                  <span className="usage-mono-name">{item.model}</span>
                  <div className="usage-row-bar" aria-hidden="true">
                    <span style={{ width: `${Math.max(item.total_tokens > 0 ? 2 : 0, share)}%` }} />
                  </div>
                </th>
                <td>
                  <div>{count(item.attempts)} 次</div>
                  <div className="usage-cell-sub">
                    {formatRate(item.success_rate)}
                    {item.retry_attempts > 0 ? ` · 重试 ${count(item.retry_attempts)}` : ""}
                  </div>
                </td>
                <td>
                  <div>
                    {compactCount(item.input_tokens)} / {compactCount(item.output_tokens)}
                  </div>
                  {item.reasoning_tokens > 0 ? (
                    <div className="usage-cell-sub">含推理 {compactCount(item.reasoning_tokens)}</div>
                  ) : null}
                </td>
                <td>
                  <strong>{count(item.total_tokens)}</strong>
                  {totalTokens > 0 ? <div className="usage-cell-sub">{percentFormat.format(item.total_tokens / denom)}</div> : null}
                </td>
                <td>
                  <div>{formatLatency(item.avg_latency_ms)}</div>
                  <div className="usage-cell-sub">P95 {formatLatency(item.p95_latency_ms)}</div>
                </td>
                <td>{formatThroughput(item.avg_tokens_per_second)}</td>
                <td>{formatRate(item.cache_hit_rate)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ScopesTable({
  scopes,
  totalTokens,
}: {
  scopes: readonly ModelScopeEntry[];
  totalTokens: number;
}): ReactElement {
  const denom = Math.max(totalTokens, 1);
  return (
    <div className="usage-table-wrap">
      <table className="usage-data-table" aria-label="按认知模块统计明细">
        <thead>
          <tr>
            <th scope="col">认知模块</th>
            <th scope="col">请求 / 调用</th>
            <th scope="col">输入 / 输出</th>
            <th scope="col">总 Token（占比）</th>
            <th scope="col">平均 / P95 延迟</th>
            <th scope="col">缓存命中</th>
          </tr>
        </thead>
        <tbody>
          {scopes.map((item) => {
            const share = totalTokens > 0 ? (item.total_tokens / denom) * 100 : 0;
            return (
              <tr key={item.scope}>
                <th scope="row" className="usage-col-name">
                  <span>{scopeLabel(item.scope)}</span>
                  <div className="usage-row-bar" aria-hidden="true">
                    <span style={{ width: `${Math.max(item.total_tokens > 0 ? 2 : 0, share)}%` }} />
                  </div>
                </th>
                <td>
                  <div>
                    {count(item.requests)} / {count(item.attempts)}
                  </div>
                  {item.retry_attempts > 0 ? <div className="usage-cell-sub">重试 {count(item.retry_attempts)}</div> : null}
                </td>
                <td>
                  {compactCount(item.input_tokens)} / {compactCount(item.output_tokens)}
                </td>
                <td>
                  <strong>{count(item.total_tokens)}</strong>
                  {totalTokens > 0 ? <div className="usage-cell-sub">{percentFormat.format(item.total_tokens / denom)}</div> : null}
                </td>
                <td>
                  <div>{formatLatency(item.avg_latency_ms)}</div>
                  <div className="usage-cell-sub">P95 {formatLatency(item.p95_latency_ms)}</div>
                </td>
                <td>{formatRate(item.cache_hit_rate)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RecentAttemptsTable({ attempts }: { attempts: readonly ModelRecentAttempt[] }): ReactElement {
  return (
    <div className="usage-table-wrap">
      <table className="usage-data-table" aria-label="最近调用记录">
        <thead>
          <tr>
            <th scope="col">时间</th>
            <th scope="col">模型</th>
            <th scope="col">模块</th>
            <th scope="col">输入 / 输出</th>
            <th scope="col">总 Token</th>
            <th scope="col">延迟</th>
            <th scope="col">状态</th>
          </tr>
        </thead>
        <tbody>
          {attempts.map((att) => {
            const status = outcomeState(att.outcome);
            return (
              <tr key={att.attempt_id}>
                <th scope="row" className="usage-col-time">
                  <div>{time(att.created_at)}</div>
                  {att.sequence > 1 ? <div className="usage-cell-sub">第 {att.sequence} 次尝试</div> : null}
                </th>
                <td className="usage-col-name">
                  <span className="usage-mono-name">{att.model}</span>
                </td>
                <td className="usage-col-text">{scopeLabel(att.scope)}</td>
                <td>
                  {att.has_usage ? `${compactCount(att.input_tokens)} / ${compactCount(att.output_tokens)}` : "—"}
                </td>
                <td>{att.has_usage ? count(att.total_tokens) : "—"}</td>
                <td>{formatLatency(att.latency_ms)}</td>
                <td className="usage-col-status">
                  <StateMark state={status.state} label={status.label} live={status.state === "running"} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ModelUsagePage({
  client,
  botName = "",
  go,
}: {
  client: PanelClient;
  botName?: string;
  go?: (route: Route) => void;
}): ReactElement {
  const [period, setPeriod] = useState<ModelUsagePeriod>("7d");
  const [chartMode, setChartMode] = useState<ChartMode>("tokens");
  const [detailTab, setDetailTab] = useState<DetailTab>("models");
  const [hoveredBucket, setHoveredBucket] = useState<number | null>(null);
  const [usage, setUsage] = useState<ModelUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestVersion = useRef(0);

  const loadUsage = useCallback(
    (targetPeriod: ModelUsagePeriod): void => {
      const version = ++requestVersion.current;
      setLoading(true);
      setError(null);
      void client
        .modelUsage(targetPeriod)
        .then((next) => {
          if (requestVersion.current !== version) return;
          setUsage(next);
          setHoveredBucket(null);
          setLoading(false);
        })
        .catch((reason: unknown) => {
          if (requestVersion.current !== version) return;
          setError(reason instanceof Error ? reason.message : "读取模型用量失败。");
          setLoading(false);
        });
    },
    [client],
  );

  useEffect(() => {
    loadUsage(period);
  }, [loadUsage, period]);

  const activeBucket =
    usage !== null && hoveredBucket !== null && usage.series[hoveredBucket] !== undefined
      ? usage.series[hoveredBucket]!
      : null;

  const subjectHint = botName.trim() ? `${botName.trim()} 的模型调用、吞吐趋势与响应延迟统计。` : "查看本地记录的模型调用次数、Token 吞吐趋势与响应延迟。";

  return (
    <div className="usage-desk">
      <PageHeading
        title="API 统计"
        aside={
          <div className="usage-head-controls">
            <fieldset className="toolbar usage-period-bar" aria-label="统计时间范围">
              <legend className="visually-hidden">统计时间范围</legend>
              {PERIODS.map((option) => (
                <FilterPill
                  key={option.value}
                  group="model-usage-period"
                  value={option.value}
                  label={option.label}
                  checked={period === option.value}
                  onChange={() => setPeriod(option.value)}
                />
              ))}
            </fieldset>
            <Button variant="secondary" disabled={loading} onClick={() => loadUsage(period)}>
              刷新
            </Button>
          </div>
        }
      >
        <p className="page-intro">
          {subjectHint}
          {usage && usage.overview.observed_at > 0 && !loading
            ? ` 更新于 ${relative(usage.overview.observed_at, Date.now())}。`
            : ""}
        </p>
      </PageHeading>

      {error !== null ? (
        <Feedback state="failed" scope="region" urgency="polite" persistence="until-resolved" title="无法读取统计数据">
          {error}
        </Feedback>
      ) : null}

      {usage === null && loading && error === null ? (
        <p className="muted" role="status">
          正在读取用量统计…
        </p>
      ) : null}

      {usage !== null ? (
        <section className="usage-summary-panel" aria-label="核心指标汇总" aria-busy={loading}>
          <div className="usage-hero-strip">
            <div className="usage-kpi-cell">
              <span className="usage-kpi-label">总 Token</span>
              <strong className="usage-kpi-value">{count(usage.total.total_tokens)}</strong>
              <span className="usage-kpi-sub">
                输入 {compactCount(usage.total.input_tokens)} · 输出 {compactCount(usage.total.output_tokens)}
              </span>
            </div>

            <div className="usage-kpi-cell">
              <span className="usage-kpi-label">调用与成功率</span>
              <strong className="usage-kpi-value">{count(usage.total.attempts)}</strong>
              <span className="usage-kpi-sub">
                {count(usage.overview.requests)} 请求 · 成功率 {formatRate(usage.overview.success_rate)}
              </span>
            </div>

            <div className="usage-kpi-cell">
              <span className="usage-kpi-label">平均延迟</span>
              <strong className="usage-kpi-value">{formatLatency(usage.overview.avg_latency_ms)}</strong>
              <span className="usage-kpi-sub">
                P50 {formatLatency(usage.overview.p50_latency_ms)} · P95 {formatLatency(usage.overview.p95_latency_ms)}
              </span>
            </div>

            <div className="usage-kpi-cell">
              <span className="usage-kpi-label">生成速率</span>
              <strong className="usage-kpi-value">{formatThroughput(usage.overview.avg_tokens_per_second)}</strong>
              <span className="usage-kpi-sub">峰值延迟 {formatLatency(usage.overview.max_latency_ms)}</span>
            </div>

            <div className="usage-kpi-cell">
              <span className="usage-kpi-label">缓存命中率</span>
              <strong className="usage-kpi-value">{formatRate(usage.total.cache_hit_rate)}</strong>
              <span className="usage-kpi-sub">命中 {compactCount(usage.total.cached_input_tokens)} tok</span>
            </div>
          </div>

          <dl className="usage-sub-strip">
            <div>
              <dt>含回执调用</dt>
              <dd>
                {count(usage.total.reported_attempts)} / {count(usage.total.attempts)}
              </dd>
            </div>
            <div>
              <dt>缓存命中输入</dt>
              <dd>{count(usage.total.cached_input_tokens)}</dd>
            </div>
            <div>
              <dt>缓存新建写入</dt>
              <dd>{count(usage.total.cache_write_input_tokens)}</dd>
            </div>
            <div>
              <dt>推理子项 Token</dt>
              <dd>{count(usage.total.reasoning_tokens)}</dd>
            </div>
            <div>
              <dt>重试 / 异常</dt>
              <dd>
                {count(usage.overview.retry_attempts)} / {count(usage.overview.failed_attempts)}
              </dd>
            </div>
          </dl>
        </section>
      ) : null}

      {usage !== null && usage.total.attempts === 0 ? (
        <EmptyNote
          icon="activity"
          title="所选时间范围内暂无调用记录"
          action={
            go ? (
              <div className="actions">
                <Button variant="secondary" onClick={() => go({ page: "chat", id: null })}>
                  前往对话
                </Button>
              </div>
            ) : undefined
          }
        >
          发起模型请求后，这里会展示按时段聚合的用量趋势、平均与 P95 延迟以及各模型明细。
        </EmptyNote>
      ) : null}

      {usage !== null && usage.total.attempts > 0 ? (
        <>
          {/* 时序趋势图 */}
          <section className="usage-chart-panel" aria-labelledby="usage-trend-heading">
            <div className="usage-panel-head">
              <div className="usage-panel-title-group">
                <h2 id="usage-trend-heading">趋势分布</h2>
                <span className="usage-readout-inline" aria-live="polite">
                  {activeBucket !== null
                    ? `${formatBucketRange(activeBucket, usage.overview.bucket_seconds)} · ${count(activeBucket.total_tokens)} tok · ${count(activeBucket.attempts)} 次调用 · 均值 ${formatLatency(activeBucket.avg_latency_ms)} / P95 ${formatLatency(activeBucket.p95_latency_ms)}`
                    : "悬停或聚焦柱体可查看具体时段数据"}
                </span>
              </div>

              <fieldset className="toolbar" aria-label="趋势指标切换">
                <legend className="visually-hidden">趋势指标切换</legend>
                {CHART_MODES.map((m) => (
                  <FilterPill
                    key={m.value}
                    group="model-usage-chart-mode"
                    value={m.value}
                    label={m.label}
                    checked={chartMode === m.value}
                    onChange={() => setChartMode(m.value)}
                  />
                ))}
              </fieldset>
            </div>

            <UsageTrendChart
              series={usage.series}
              bucketSeconds={usage.overview.bucket_seconds}
              mode={chartMode}
              activeIndex={hoveredBucket}
              onSelectIndex={setHoveredBucket}
            />

            <div className="usage-chart-legend">
              {chartMode === "tokens" ? (
                <>
                  <span>
                    <i className="usage-dot usage-dot-soft" aria-hidden="true" />
                    输入 Token
                  </span>
                  <span>
                    <i className="usage-dot usage-dot-primary" aria-hidden="true" />
                    输出 Token
                  </span>
                </>
              ) : chartMode === "latency" ? (
                <>
                  <span>
                    <i className="usage-dot usage-dot-primary" aria-hidden="true" />
                    实线：平均延迟
                  </span>
                  <span>
                    <i className="usage-dot usage-dot-muted" aria-hidden="true" />
                    虚线：P95 延迟
                  </span>
                </>
              ) : (
                <>
                  <span>
                    <i className="usage-dot usage-dot-primary" aria-hidden="true" />
                    正常完成
                  </span>
                  {usage.overview.failed_attempts > 0 ? (
                    <span>
                      <i className="usage-dot usage-dot-fail" aria-hidden="true" />
                      超时或失败
                    </span>
                  ) : null}
                </>
              )}
            </div>
          </section>

          {/* 统计明细表：按模型 / 按认知模块 / 最近记录 */}
          <section className="usage-detail-section" aria-labelledby="usage-detail-heading">
            <div className="usage-panel-head">
              <h2 id="usage-detail-heading">统计明细</h2>
              <fieldset className="toolbar" aria-label="明细维度切换">
                <legend className="visually-hidden">明细维度切换</legend>
                <FilterPill
                  group="model-usage-detail-tab"
                  value="models"
                  label={`按模型 (${usage.model_metrics.length})`}
                  checked={detailTab === "models"}
                  onChange={() => setDetailTab("models")}
                />
                <FilterPill
                  group="model-usage-detail-tab"
                  value="scopes"
                  label={`按模块 (${usage.scopes.length})`}
                  checked={detailTab === "scopes"}
                  onChange={() => setDetailTab("scopes")}
                />
                <FilterPill
                  group="model-usage-detail-tab"
                  value="recent"
                  label={`最近记录 (${usage.recent_attempts.length})`}
                  checked={detailTab === "recent"}
                  onChange={() => setDetailTab("recent")}
                />
              </fieldset>
            </div>

            {detailTab === "models" ? (
              <ModelsTable models={usage.model_metrics} totalTokens={usage.total.total_tokens} />
            ) : detailTab === "scopes" ? (
              <ScopesTable scopes={usage.scopes} totalTokens={usage.total.total_tokens} />
            ) : (
              <RecentAttemptsTable attempts={usage.recent_attempts} />
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}
