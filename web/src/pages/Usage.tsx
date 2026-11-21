import { useEffect, useState } from "react";
import { api, UsageSummary } from "../api";

const PAGE_SIZE = 50;

// 用量页的数字统一带千分位，避免长 token 数难读
const fmt = (value: number) => value.toLocaleString();

/**
 * 用量监控页面：展示 AI 模型调用的 token 用量、延迟、失败率等指标。
 * 支持按天/模型/用户三种维度聚合,仅运营人员/管理员可访问。
 */
export default function Usage() {
  const [data, setData] = useState<UsageSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [groupBy, setGroupBy] = useState<"day" | "model" | "user">("day");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    loadData();
  }, [groupBy, page]);

  async function loadData() {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listUsage({
        group_by: groupBy,
        from: from || undefined,
        to: to || undefined,
        page,
        size: PAGE_SIZE,
      });
      setData(response.items);
      setTotal(response.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  function handleFilter() {
    setPage(1);
    loadData();
  }

  // 当前页汇总：平均延迟按调用次数加权，P95 取当前页最大值（非全量分位）
  const totals = data.reduce(
    (acc, row) => {
      acc.calls += row.calls;
      acc.tokens += row.total_tokens;
      acc.errors += row.errors;
      acc.latency += row.avg_latency_ms * row.calls;
      acc.p95 = Math.max(acc.p95, row.p95_latency_ms);
      return acc;
    },
    { calls: 0, tokens: 0, errors: 0, latency: 0, p95: 0 },
  );
  const avgLatency = totals.calls ? Math.round(totals.latency / totals.calls) : 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const keyLabel = groupBy === "day" ? "日期" : groupBy === "model" ? "模型" : "用户";

  return (
    <div>
      <div className="page-head">
        <div>
          <p className="eyebrow">Observability</p>
          <h1>用量监控</h1>
          <p className="muted">按天、模型或用户查看 AI 调用的 token 消耗、失败数与延迟，快速定位异常调用。</p>
        </div>
        <div className="page-head-actions">
          <span className="chip">{keyLabel}维度</span>
          <span className="chip">共 {fmt(total)} 条</span>
        </div>
      </div>

      {error && <p className="error">{error}</p>}

      <div className="metrics">
        <div className="metric"><span>调用次数</span><strong>{fmt(totals.calls)}</strong></div>
        <div className="metric"><span>Total Tokens</span><strong>{fmt(totals.tokens)}</strong></div>
        <div className="metric"><span>失败数</span><strong>{fmt(totals.errors)}</strong></div>
        <div className="metric"><span>平均延迟</span><strong>{fmt(avgLatency)}ms</strong></div>
        <div className="metric"><span>P95 延迟（当前页峰值）</span><strong>{fmt(totals.p95)}ms</strong></div>
      </div>

      <div className="panel">
        <h2>筛选</h2>
        <div className="filters">
          <label>
            聚合维度
            <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as "day" | "model" | "user")}>
              <option value="day">按天</option>
              <option value="model">按模型</option>
              <option value="user">按用户</option>
            </select>
          </label>
          <label>
            开始时间
            <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label>
            结束时间
            <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
          <div className="filters-actions">
            <button className="primary small" onClick={handleFilter}>查询</button>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2>调用明细</h2>
          <span className="hint">第 {page} / {totalPages} 页</span>
        </div>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{keyLabel}</th>
                <th className="num">调用次数</th>
                <th className="num">Prompt</th>
                <th className="num">Completion</th>
                <th className="num">Total Tokens</th>
                <th className="num">失败数</th>
                <th className="num">平均延迟</th>
                <th className="num">P95 延迟</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={8} className="table-empty">加载中…</td></tr>
              ) : data.length === 0 ? (
                <tr><td colSpan={8} className="table-empty">暂无数据</td></tr>
              ) : (
                data.map((item) => (
                  <tr key={item.key}>
                    <td>{item.key}</td>
                    <td className="num">{fmt(item.calls)}</td>
                    <td className="num">{fmt(item.prompt_tokens)}</td>
                    <td className="num">{fmt(item.completion_tokens)}</td>
                    <td className="num">{fmt(item.total_tokens)}</td>
                    <td className={item.errors > 0 ? "num bad" : "num"}>{fmt(item.errors)}</td>
                    <td className="num">{fmt(item.avg_latency_ms)}ms</td>
                    <td className="num">{fmt(item.p95_latency_ms)}ms</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* 分页只做上一页/下一页，够用且避免页码控件复杂度 */}
        {total > PAGE_SIZE && (
          <div className="row-actions">
            <button className="ghost small" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
            <span className="hint">第 {page} / {totalPages} 页 · 共 {fmt(total)} 条</span>
            <button className="ghost small" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>下一页</button>
          </div>
        )}
      </div>
    </div>
  );
}
