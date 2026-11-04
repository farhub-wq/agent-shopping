import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, getSession, money } from "../lib/api";

// 后端状态码到中文的展示映射；未命中的状态回退显示原始码，避免悄悄丢信息。
const STATUS_LABELS: Record<string, string> = {
  CREATED: "已创建",
  RESERVED: "库存已锁定",
  PAYING: "支付中",
  PAID: "已支付",
  SHIPPED: "已发货",
  COMPLETED: "已完成",
  CANCELLED: "已取消",
  REFUNDING: "退款处理中",
  REFUNDED: "已退款",
};

const REASONS = [
  { value: "quality_issue", label: "质量问题" },
  { value: "size_issue", label: "尺码不合适" },
  { value: "wrong_item", label: "发错货" },
  { value: "no_return_refund", label: "仅退款" },
  { value: "price_protection", label: "价保" },
];

export default function Orders() {
  const session = getSession();
  const [tab, setTab] = useState<"orders" | "tickets">("orders");
  const [orders, setOrders] = useState<any[]>([]);
  const [tickets, setTickets] = useState<any[]>([]);
  const [timeline, setTimeline] = useState<any>(null);
  const [formOrder, setFormOrder] = useState<string | null>(null);
  const [reason, setReason] = useState("quality_issue");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");

  // 订单与售后单是两个独立列表，一次并发拉取减少往返。
  const load = async () => {
    try {
      const [orderData, ticketData] = await Promise.all([api.orders(), api.tickets()]);
      setOrders(orderData.items || []);
      setTickets(ticketData.items || []);
      setError("");
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  // 未登录不请求数据，直接展示引导；登录态在进入页面时读取一次即可。
  useEffect(() => {
    if (session) load();
  }, []);

  if (!session) {
    return (
      <div className="empty">
        <h2>请先登录</h2>
        <p className="muted">点击右上角「身份登录」以顾客身份进入。</p>
        <Link className="primary" to="/">返回商城</Link>
      </div>
    );
  }

  const pay = async (orderId: string) => {
    try {
      await api.pay(orderId);
      // 支付会推进订单状态，必须重新拉列表而不是本地改字段。
      await load();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const showTimeline = async (orderId: string) => {
    try {
      // 轨迹由订单服务按 OrderEvent 汇总返回，前端只负责展示。
      setTimeline(await api.timeline(orderId));
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const submitTicket = async (orderId: string) => {
    try {
      // 顾客侧只发起「仅退款」申请，审批与退款由运营在后台完成。
      await api.createTicket({ order_id: orderId, reason, description, type: "refund_only" });
      setFormOrder(null);
      setDescription("");
      await load();
      setTab("tickets");
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <div>
      <div className="tabs">
        <button className={tab === "orders" ? "chip active" : "chip"} onClick={() => setTab("orders")}>我的订单</button>
        <button className={tab === "tickets" ? "chip active" : "chip"} onClick={() => setTab("tickets")}>售后单</button>
      </div>
      {error && <p className="error">{error}</p>}

      {tab === "orders" && (
        <div className="list">
          {orders.map((order) => (
            <article className="panel order" key={order.id}>
              <header>
                <div>
                  <strong>{order.order_no}</strong>
                  <span className={`status ${order.status.toLowerCase()}`}>{STATUS_LABELS[order.status] || order.status}</span>
                </div>
                <strong className="price">{money(order.amount_cents)}</strong>
              </header>
              <ul className="items">
                {order.items.map((item: any) => (
                  <li key={item.sku}>
                    <span>{item.title}</span>
                    <span className="muted">{item.sku} × {item.qty}</span>
                  </li>
                ))}
              </ul>
              <div className="row-actions">
                {/* 操作入口由订单状态驱动：仅 RESERVED 可支付，已支付之后才允许售后。 */}
                {order.status === "RESERVED" && <button className="primary small" onClick={() => pay(order.id)}>去支付</button>}
                {(order.status === "PAID" || order.status === "SHIPPED" || order.status === "COMPLETED") && (
                  <button className="ghost" onClick={() => setFormOrder(order.id)}>申请售后</button>
                )}
                <button className="ghost" onClick={() => showTimeline(order.id)}>状态轨迹</button>
              </div>
              {formOrder === order.id && (
                <div className="inline-form">
                  <select value={reason} onChange={(event) => setReason(event.target.value)}>
                    {REASONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                  </select>
                  <textarea
                    placeholder="补充说明，例如「尺码偏小」「收到时外包装破损」"
                    value={description}
                    onChange={(event) => setDescription(event.target.value)}
                  />
                  <div className="row-actions">
                    <button className="primary small" onClick={() => submitTicket(order.id)}>提交售后</button>
                    <button className="ghost" onClick={() => setFormOrder(null)}>取消</button>
                  </div>
                </div>
              )}
            </article>
          ))}
          {!orders.length && <p className="muted center">还没有订单，去商城下单试试。</p>}
        </div>
      )}

      {tab === "tickets" && (
        <div className="list">
          {tickets.map((ticket) => (
            <article className="panel order" key={ticket.id}>
              <header>
                <div>
                  <strong>{ticket.ticket_no}</strong>
                  <span className={`status ${ticket.status}`}>{ticket.status}</span>
                </div>
                <strong className="price">{money(ticket.amount_cents)}</strong>
              </header>
              <p className="muted">订单 {ticket.order_id} · 原因 {ticket.reason}</p>
              <p>{ticket.decision_note}</p>
              {/* 风险评分与命中原因由售后规则计算，存在才展示，供用户理解审核依据。 */}
              {ticket.risk_reasons?.length > 0 && (
                <p className="hint">风险：{ticket.risk_reasons.join("；")}（评分 {ticket.risk_score}）</p>
              )}
            </article>
          ))}
          {!tickets.length && <p className="muted center">暂无售后单。</p>}
        </div>
      )}

      {timeline && (
        <div className="panel">
          <div className="panel-head">
            <h2>状态轨迹 · {timeline.status_label}</h2>
            <button className="ghost" onClick={() => setTimeline(null)}>关闭</button>
          </div>
          <ol className="timeline">
            {timeline.events.map((event: any, index: number) => (
              <li key={index}>
                <strong>{event.from} → {event.to}</strong>
                <span className="muted">事件 {event.event} · 操作者 {event.actor}</span>
                <small>{new Date(event.at).toLocaleString()}</small>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
