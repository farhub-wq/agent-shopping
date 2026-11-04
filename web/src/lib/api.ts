export type Role = "customer" | "operator" | "admin";

export interface Session {
  token: string;
  user_id: string;
  role: Role;
}

// 登录态与购物车都只落在 localStorage，前端不保存任何服务端会话副本。
const SESSION_KEY = "shop_session";
const CART_KEY = "shop_cart";

export interface CartItem {
  sku: string;
  product_id: string;
  title: string;
  price_cents: number;
  qty: number;
}

export function getSession(): Session | null {
  const raw = localStorage.getItem(SESSION_KEY);
  return raw ? (JSON.parse(raw) as Session) : null;
}

export function setSession(session: Session) {
  localStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

export function logout() {
  localStorage.removeItem(SESSION_KEY);
}

// 登录直接调用网关换取 token；拿到后立刻写回本地，后续请求统一读这份 session。
export async function login(userId: string, role: Role): Promise<Session> {
  const response = await fetch("/api/auth/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, role }),
  });
  if (!response.ok) throw new Error("登录失败");
  const data = await response.json();
  const session: Session = { token: data.token, user_id: data.user_id, role: data.role };
  setSession(session);
  return session;
}

/**
 * 全站唯一的 HTTP 出口：统一拼 /api 前缀、注入登录态、解析错误响应。
 * 页面组件不得自行 fetch，避免路径与鉴权头散落各处。
 */
async function request<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const session = getSession();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string> | undefined),
  };
  // 已登录时把 token 放进 Authorization；未登录请求走访客权限上限。
  if (session) headers.Authorization = `Bearer ${session.token}`;
  const response = await fetch(`/api${path}`, { ...options, headers });
  // 后端错误体约定为 { code, message }，这里优先透出 message 供页面展示。
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(body.message || `请求失败 (${response.status})`);
  }
  return body as T;
}

export const api = {
  feed: (params: { q?: string; category?: string } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, value]) => value) as [string, string][],
    ).toString();
    return request(`/storefront/feed${query ? `?${query}` : ""}`);
  },
  product: (productId: string) => request(`/storefront/products/${productId}`),
  // 下单必须带 Idempotency-Key，由调用方生成，保证重复提交不会产生重复订单。
  checkout: (payload: any, idempotencyKey: string) =>
    request("/storefront/checkout", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    }),
  orders: (status?: string) => request(`/storefront/orders${status ? `?status=${status}` : ""}`),
  pay: (orderId: string) =>
    request(`/storefront/orders/${orderId}/pay`, { method: "POST", body: JSON.stringify({ method: "mock" }) }),
  timeline: (orderId: string) => request(`/orders/${orderId}/timeline`),
  inventory: () => request("/inventory/stocks"),
  setStock: (sku: string, total: number) =>
    request("/inventory/stocks", { method: "POST", body: JSON.stringify({ sku, total }) }),
  shipments: (status?: string) => request(`/fulfillment/shipments${status ? `?status=${status}` : ""}`),
  ship: (shipmentId: string) =>
    request(`/fulfillment/shipments/${shipmentId}/ship`, {
      method: "POST",
      body: JSON.stringify({ carrier: "SF" }),
    }),
  deliver: (shipmentId: string) =>
    request(`/fulfillment/shipments/${shipmentId}/deliver`, { method: "POST", body: "{}" }),
  tickets: (status?: string) => request(`/aftersales/tickets${status ? `?status=${status}` : ""}`),
  createTicket: (payload: any) =>
    request("/aftersales/tickets", { method: "POST", body: JSON.stringify(payload) }),
  approveTicket: (ticketId: string) =>
    request(`/aftersales/tickets/${ticketId}/approve`, { method: "POST", body: JSON.stringify({ note: "人工审核通过" }) }),
  rejectTicket: (ticketId: string) =>
    request(`/aftersales/tickets/${ticketId}/reject`, { method: "POST", body: JSON.stringify({ note: "人工驳回" }) }),
  refundTicket: (ticketId: string) =>
    request(`/aftersales/tickets/${ticketId}/refund-complete`, { method: "POST", body: "{}" }),
  agents: () => request("/agents"),
  policies: () => request("/policies"),
  evidence: (runId: string) => request(`/agents/runs/${runId}/evidence`),
  opsOrders: (status?: string) => request(`/orders${status ? `?status=${status}` : ""}`),
  memory: (userId: string) => request(`/agents/memory/${userId}`),
};

export function cartItems(): CartItem[] {
  const raw = localStorage.getItem(CART_KEY);
  return raw ? (JSON.parse(raw) as CartItem[]) : [];
}

export function saveCart(items: CartItem[]) {
  localStorage.setItem(CART_KEY, JSON.stringify(items));
  // 购物车变更通过事件广播，Header 的角标据此刷新，避免组件间直接耦合。
  window.dispatchEvent(new Event("cart-updated"));
}

export function addToCart(item: CartItem) {
  const items = cartItems();
  const existing = items.find((entry) => entry.sku === item.sku);
  if (existing) existing.qty += item.qty;
  else items.push(item);
  saveCart(items);
}

export function clearCart() {
  saveCart([]);
}

export interface StreamEvent {
  type: string;
  [key: string]: any;
}

/**
 * Agent 对话走 SSE 流式返回：逐帧解析 data: 行并回调页面。
 * 不能用 response.json()，否则会等到整段结束时才更新 UI。
 */
export async function chatStream(
  skill: string,
  message: string,
  sessionId: string,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const session = getSession();
  const response = await fetch(`/api/agents/${skill}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(session ? { Authorization: `Bearer ${session.token}` } : {}),
    },
    body: JSON.stringify({ message, session_id: sessionId }),
  });
  if (!response.ok || !response.body) throw new Error("Agent 连接失败");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // 按空行切分 SSE 事件；最后一段可能不完整，留在 buffer 等下一帧再拼。
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      const line = chunk.trim();
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") return;
      try {
        onEvent(JSON.parse(payload));
      } catch {
        /* 忽略不完整分片 */
      }
    }
  }
}

// 接口金额一律以“分”传输，展示时统一在此换算，避免各页面重复处理精度。
export function money(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}
