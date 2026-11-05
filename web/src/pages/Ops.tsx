import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, getSession, money } from "../lib/api";

type Tab = "inventory" | "orders" | "shipments" | "tickets";

export default function Ops() {
  const session = getSession();
  const [tab, setTab] = useState<Tab>("inventory");
  const [rows, setRows] = useState<any[]>([]);
  const [error, setError] = useState("");

  // 不同 tab 对应不同服务接口，切换后按当前 tab 重新取数，避免残留旧列表。
  const load = async () => {
    try {
      setError("");
      if (tab === "inventory") setRows((await api.inventory()).items || []);
      else if (tab === "orders") setRows((await api.opsOrders()).items || []);
      else if (tab === "shipments") setRows((await api.shipments()).items || []);
      else setRows((await api.tickets("pending")).items || []);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  // 只有运营/管理员才请求数据；顾客身份直接走下面的拦截页。
  // 依赖里带上登录身份：退出后在同一页面重新登录时，仅 tab 不变不会触发 effect，
  // 会导致列表继续停留在旧会话的空数据上。
  useEffect(() => {
    if (session && session.role !== "customer") {
      // 各 tab 的数据结构不同，切换时先清空，避免用上一 tab 的行渲染（如库存行没有 order_id）
      setRows([]);
      load();
    }
  }, [tab, session?.user_id, session?.role]);

  // 前端仅做入口收敛，真正的权限校验由网关与服务端再验一次。
  if (!session || session.role === "customer") {
    return (
      <div className="empty">
        <h2>需要运营身份</h2>
        <p className="muted">在右上角切换身份为「运营」后进入。</p>
        <Link className="primary" to="/agent">或先体验智能助手</Link>
      </div>
    );
  }

  // 所有运营动作成功后再拉一次列表，以服务端状态为准，避免本地乐观更新失真。
  const act = async (fn: () => Promise<any>) => {
    try {
      await fn();
      await load();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <div>
      <div className="tabs">
        {([
          ["inventory", "库存"],
          ["orders", "订单"],
          ["shipments", "履约"],
          ["tickets", "售后待审"],
        ] as [Tab, string][]).map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "chip active" : "chip"}
            onClick={() => {
              // 切换 tab 与清空列表放在同一次事件里，避免 React 先用上一 tab 的数据渲染新 tab
              setTab(key);
              setRows([]);
            }}
          >
            {label}
          </button>
        ))}
        <Link className="chip" to="/agent">Agent 工作台</Link>
      </div>
      {error && <p className="error">{error}</p>}

      {tab === "inventory" && (
        <div className="panel">
          <h2>库存与预占</h2>
          <table className="table">
            <thead>
              <tr><th>SKU</th><th>总量</th><th>预占</th><th>可售</th><th>操作</th></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.sku}>
                  <td>{row.sku}</td>
                  <td>{row.total}</td>
                  <td>{row.reserved}</td>
                  <td>{row.available}</td>
                  <td>
                    <button
                      className="ghost small"
                      onClick={() => {
                        // 调整的是总库存，可售量由服务端按“总量-预占”重新计算。
                        const value = window.prompt(`设置 ${row.sku} 的总库存`, String(row.total));
                        if (value !== null) act(() => api.setStock(row.sku, Number(value)));
                      }}
                    >
                      调整
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === "orders" && (
        <div className="list">
          {rows.map((order) => (
            <article className="panel order" key={order.id}>
              <header>
                <strong>{order.order_no}</strong>
                <span className={`status ${order.status.toLowerCase()}`}>{order.status}</span>
                <span className="price">{money(order.amount_cents)}</span>
              </header>
              <p className="muted">用户 {order.user_id} · {order.items?.map((item: any) => `${item.title}×${item.qty}`).join("，")}</p>
            </article>
          ))}
        </div>
      )}

      {tab === "shipments" && (
        <div className="list">
          {rows.map((shipment) => (
            <article className="panel order" key={shipment.id}>
              <header>
                <strong>订单 {shipment.order_id.slice(0, 8)}</strong>
                <span className={`status ${shipment.status}`}>{shipment.status}</span>
                {shipment.sla_flagged && <span className="status cancelled">履约超时</span>}
              </header>
              <p className="muted">截止 {new Date(shipment.deadline_at).toLocaleString()} · 运单 {shipment.tracking_no || "-"}</p>
              <div className="row-actions">
                {/* 履约动作按状态单向推进：pending 发货，shipped 后确认签收。 */}
                {shipment.status === "pending" && (
                  <button className="primary small" onClick={() => act(() => api.ship(shipment.id))}>发货</button>
                )}
                {shipment.status === "shipped" && (
                  <button className="ghost" onClick={() => act(() => api.deliver(shipment.id))}>确认签收</button>
                )}
              </div>
            </article>
          ))}
          {!rows.length && <p className="muted center">暂无履约单。订单支付后自动生成。</p>}
        </div>
      )}

      {tab === "tickets" && (
        <div className="list">
          {rows.map((ticket) => (
            <article className="panel order" key={ticket.id}>
              <header>
                <strong>{ticket.ticket_no}</strong>
                <span className={`status ${ticket.status}`}>{ticket.status}</span>
                <span className="price">{money(ticket.amount_cents)}</span>
              </header>
              <p>{ticket.description || ticket.decision_note}</p>
              <p className="hint">风险评分 {ticket.risk_score} · {ticket.risk_reasons?.join("；") || "无"}</p>
              {/* Agent 只给建议，通过/驳回仍是人工动作，前端不自动执行。 */}
              {ticket.agent_recommendation?.recommendation && (
                <p className="advice">
                  Agent 建议：{ticket.agent_recommendation.recommendation}（置信度 {ticket.agent_recommendation.confidence}）
                  <br />
                  {ticket.agent_recommendation.rationale}
                </p>
              )}
              <div className="row-actions">
                {/* 审批动作都由人工点击触发，Agent 结果不在这里自动执行。 */}
                <button className="primary small" onClick={() => act(() => api.approveTicket(ticket.id))}>通过</button>
                <button className="ghost" onClick={() => act(() => api.rejectTicket(ticket.id))}>驳回</button>
                {/* 退款完成不可逆，只在 approved 后开放。 */}
                {ticket.status === "approved" && (
                  <button className="ghost" onClick={() => act(() => api.refundTicket(ticket.id))}>完成退款</button>
                )}
              </div>
            </article>
          ))}
          {!rows.length && <p className="muted center">没有待处理的售后单。</p>}
        </div>
      )}
    </div>
  );
}
