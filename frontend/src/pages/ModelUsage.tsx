/**
 * API 统计页按所选时间范围读取宿主的汇总。页面只持有筛选和查询状态，
 * 用量口径与历史数据由宿主的模型请求账本维护。
 */
import { useEffect, useState, type ReactElement } from "react";
import { Button, Feedback } from "@purrcept/cel";
import { HttpError, type PanelClient } from "../client";
import type { ModelUsage, ModelUsagePeriod } from "../contracts";
import { EmptyNote, FilterPill, PageHeading, Section } from "../ui/common";

const PERIODS = [
  { value: "24h", label: "最近 24 小时" },
  { value: "7d", label: "最近 7 天" },
  { value: "30d", label: "最近 30 天" },
  { value: "all", label: "全部时间" },
] as const;

const COUNTS = [
  ["input_tokens", "输入 token"],
  ["output_tokens", "输出 token"],
  ["cached_input_tokens", "缓存读取 token"],
  ["cache_write_input_tokens", "缓存写入 token"],
  ["reasoning_tokens", "推理 token"],
] as const;

const count = new Intl.NumberFormat("zh-CN");
const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 });

type LoadState =
  | { status: "loading" }
  | { status: "ready"; usage: ModelUsage }
  | { status: "error"; message: string };

/** 显示 Engine 的全局模型用量和按请求模型名汇总的明细。 */
export function ModelUsagePage({ client }: { client: PanelClient }): ReactElement {
  const [period, setPeriod] = useState<ModelUsagePeriod>("7d");
  const [refreshes, setRefreshes] = useState(0);
  const [load, setLoad] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    void client.modelUsage(period).then(
      (usage) => {
        if (active) setLoad({ status: "ready", usage });
      },
      (error: unknown) => {
        if (active) {
          const message = error instanceof HttpError ? error.message : "无法读取本机用量数据。";
          setLoad({ status: "error", message });
        }
      },
    );
    // 快速切换范围时，上一笔查询即使晚于当前查询返回，也不能覆盖当前结果。
    return () => { active = false; };
  }, [client, period, refreshes]);

  return (
    <>
      <PageHeading
        title="API 统计"
        aside={
          <Button variant="secondary" loading={load.status === "loading"} onClick={() => {
            setLoad({ status: "loading" });
            setRefreshes((value) => value + 1);
          }}>
            刷新
          </Button>
        }
      >
        汇总本机 Engine 请求账本中的生成和判断调用，token 以服务商返回的用量为准。
      </PageHeading>
      <fieldset className="toolbar usage-period">
        <legend className="visually-hidden">统计时间范围</legend>
        {PERIODS.map(({ value, label }) => (
          <FilterPill
            key={value}
            group="model-usage-period"
            value={value}
            label={label}
            checked={period === value}
            onChange={() => {
              setLoad({ status: "loading" });
              setPeriod(value);
            }}
          />
        ))}
      </fieldset>
      {load.status === "loading" ? (
        <p className="page-note" role="status">正在读取 API 统计…</p>
      ) : load.status === "error" ? (
        <Feedback state="warning" scope="region" urgency="passive" persistence="until-resolved" title="API 统计读取失败">
          {load.message}
        </Feedback>
      ) : load.usage.total.attempts === 0 ? (
        <EmptyNote icon="activity">此时间范围内暂无模型调用记录。</EmptyNote>
      ) : (
        <UsageReport usage={load.usage} />
      )}
    </>
  );
}

function UsageReport({ usage: { total, models } }: { usage: ModelUsage }): ReactElement {
  const tokens = (value: number): string => total.reported_attempts > 0 ? count.format(value) : "—";
  return (
    <>
      <Section id="model-usage-overview" title="全局用量">
        <dl className="usage-metrics">
          <div>
            <dt>总 token</dt>
            <dd>{tokens(total.total_tokens)}</dd>
          </div>
          <div>
            <dt>缓存命中率</dt>
            <dd>
              {total.cache_hit_rate === null ? "—" : percent.format(total.cache_hit_rate)}
              {total.cache_hit_rate === null ? null : (
                <meter aria-hidden="true" min={0} max={1} value={total.cache_hit_rate} />
              )}
            </dd>
          </div>
          <div>
            <dt>调用尝试</dt>
            <dd>{count.format(total.attempts)}</dd>
          </div>
          <div>
            <dt>有用量回执</dt>
            <dd>{count.format(total.reported_attempts)}</dd>
          </div>
        </dl>
        <dl className="usage-metrics usage-breakdown">
          {COUNTS.map(([field, label]) => (
            <div key={field}>
              <dt>{label}</dt>
              <dd>{tokens(total[field])}</dd>
            </div>
          ))}
        </dl>
        <p className="section-note">总 token 为输入与输出之和。缓存命中率为缓存读取 token 占已报告输入 token 的比例。调用尝试包含重试。</p>
      </Section>
      <Section id="model-usage-models" title="按模型">
        <table className="usage-table" aria-label="按模型统计">
          <thead>
            <tr>
              <th scope="col">模型</th>
              <th scope="col">调用尝试</th>
              <th scope="col">总 token</th>
              <th scope="col">缓存命中率</th>
            </tr>
          </thead>
          <tbody>
            {models.map((row) => (
              <tr key={row.model}>
                <th scope="row">{row.model}</th>
                <td>{count.format(row.attempts)}</td>
                <td>{row.reported_attempts === 0 ? "—" : count.format(row.total_tokens)}</td>
                <td>{row.cache_hit_rate === null ? "—" : percent.format(row.cache_hit_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </>
  );
}
