export type Role = "customer" | "operator" | "admin";

export interface Session {
  user_id: string;
  role: Role;
  phone: string;
}

// localStorage 只存展示用的会话快照；access token 只放内存，刷新页面后靠 Cookie 重新换
const SESSION_KEY = "shop_session";
const CART_KEY = "shop_cart";

let accessToken: string | null = null;

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

function clearSession() {
  accessToken = null;
  localStorage.removeItem(SESSION_KEY);
}

interface AuthPayload {
  access_token: string;
  expires_in: number;
  user: { id: string; phone: string; roles: Role[] };
}

// JWT 与服务端一样只认单一角色：多角色用户按最高权限角色展示与路由
function primaryRole(roles: Role[]): Role {
  if (roles.includes("admin")) return "admin";
  if (roles.includes("operator")) return "operator";
  return "customer";
}

function applyAuth(data: AuthPayload): Session {
  accessToken = data.access_token;
  const session: Session = { user_id: data.user.id, phone: data.user.phone, role: primaryRole(data.user.roles) };
  setSession(session);
  return session;
}

async function readError(response: Response): Promise<string> {
  const body = (await response.json().catch(() => ({}))) as { message?: string };
  return body.message || `请求失败 (${response.status})`;
}

// 登录只提交手机号与密码；角色永远由服务端按 DB 下发，前端不传角色
export async function login(phone: string, password: string): Promise<Session> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone, password }),
  });
  if (!response.ok) throw new Error(await readError(response));
  return applyAuth((await response.json()) as AuthPayload);
}

export async function register(phone: string, password: string): Promise<void> {
  const response = await fetch("/api/auth/register", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone, password }),
  });
  if (!response.ok) throw new Error(await readError(response));
}

// 双提交 CSRF：读非 HttpOnly 的 shop_csrf Cookie，刷新/登出时回放到 Header
export function csrfToken(): string | null {
  const match = document.cookie.match(/(?:^|;\s*)shop_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

export async function refreshSession(): Promise<Session | null> {
  const csrf = csrfToken();
  if (!csrf) return null;
  const response = await fetch("/api/auth/refresh", {
    method: "POST",
    credentials: "include",
    headers: { "X-CSRF-Token": csrf },
  });
  if (!response.ok) {
    clearSession();
    return null;
  }
  return applyAuth((await response.json()) as AuthPayload);
}

export async function logout(): Promise<void> {
  const csrf = csrfToken();
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "include",
      headers: csrf ? { "X-CSRF-Token": csrf } : {},
    });
  } finally {
    // 无论服务端是否成功，本地会话都必须清掉，避免残留身份展示
    clearSession();
  }
}

let refreshPromise: Promise<Session | null> | null = null;

// 刷新页面后 access token 只存在内存会丢失，但本地 session 还在。
// 此时先静默换一次 token，否则运营接口会以匿名身份返回 403，页面只显示空列表。
// 用共享 Promise 合并并发请求的刷新，避免同时发多个 /auth/refresh。
async function ensureAccessToken(): Promise<void> {
  if (accessToken || !getSession()) return;
  refreshPromise ??= refreshSession().finally(() => {
    refreshPromise = null;
  });
  await refreshPromise;
}

/**
 * 全站唯一的 HTTP 出口：统一拼 /api 前缀、注入登录态、解析错误响应。
 * 页面组件不得自行 fetch，避免路径与鉴权头散落各处。
 */
async function request<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string> | undefined),
  };
  await ensureAccessToken();
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  let response = await fetch(`/api${path}`, { ...options, credentials: "include", headers });
  // access token 过期时只在已登录状态下自动刷新一次并重放；刷新失败会话已被清空，原错误继续抛出
  if (response.status === 401 && getSession()) {
    const restored = await refreshSession();
    if (restored) {
      headers.Authorization = `Bearer ${accessToken}`;
      response = await fetch(`/api${path}`, { ...options, credentials: "include", headers });
    }
  }
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
  // 流式接口同样要保证 token 已恢复，否则刷新页面后首条消息会以匿名身份被拒
  await ensureAccessToken();
  const response = await fetch(`/api/agents/${skill}/chat`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
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
