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

// 审计日志：与 audit 服务 /audit/logs 响应一致，before/after 为脱敏后的变更快照
export interface AuditLog {
  id: string;
  occurred_at: string;
  service: string;
  actor_id: string;
  actor_role: string;
  tenant_id: string;
  action: string;
  target_type: string;
  target_id: string;
  request_id: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  ip_hash: string;
}

export interface AuditLogPage {
  items: AuditLog[];
  page: number;
  size: number;
  total: number;
}

// 拒付（chargeback）：payment 服务 /payments/chargebacks 响应；金额单位为分
export interface Chargeback {
  id: string;
  payment_id: string;
  order_id: string;
  provider_case_id: string;
  amount_cents: number;
  currency: string;
  status: "opened" | "won" | "lost";
  reason: string;
  opened_at: string;
  closed_at: string | null;
}

export interface ChargebackPage {
  items: Chargeback[];
  page: number;
  size: number;
  total: number;
}

// ======== Agent 多模型接入相关类型 ========

// 模型提供商预设
export interface ProviderPreset {
  key: string;
  type: string;
  name: string;
  base_url: string;
  default_model: string;
  key_label: string;
}

// 模型提供商
export interface ModelProvider {
  id: string;
  owner_user_id: string | null;
  tenant_id: string;
  provider_type: string;
  name: string;
  base_url: string;
  model: string;
  is_default: boolean;
  enabled: boolean;
  has_key: boolean;
  key_masked: string;
  created_at: string;
  updated_at: string;
}

// 会话
export interface Conversation {
  id: string;
  skill: string;
  title: string;
  // 父子拆分链：root_id 指向链路起点，parent_id 指向被拆分的旧会话
  root_id: string | null;
  parent_id: string | null;
  // 会话累计 token 与上下文状态（ok | warn | split）
  token_count: number;
  context_state: string;
  summary_id: string | null;
  // 后端建议拆分的原因：context（累计 token 到阈值）| idle（空闲超时）
  split_suggested: string | null;
  created_at: string;
  last_message_at: string;
}

export interface ConversationPage {
  items: Conversation[];
  page: number;
  size: number;
  total: number;
}

// 消息
export interface Message {
  id: string;
  role: string;
  content: string;
  tool_calls: string | null;
  seq: number;
  created_at: string;
}

export interface MessagePage {
  items: Message[];
  // 向前翻更早历史的分页游标；为空表示没有更早的消息
  next_cursor: string | null;
  has_more: boolean;
}

// 用量统计
export interface UsageSummary {
  key: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
  errors: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
}

export interface UsagePage {
  items: UsageSummary[];
  page: number;
  size: number;
  total: number;
}

// ======== 业务领域类型：与各服务响应字段对齐，组件只消费这些收窄后的类型 ========

// 商城首页商品（storefront/feed）
export interface FeedItem {
  id: string;
  image_url: string;
  title: string;
  category: string;
  subtitle: string;
  price_cents: number;
  in_stock: boolean;
}

// 商品详情 + 库存快照（storefront/products/:id）
export interface ProductDetail {
  id: string;
  sku: string;
  title: string;
  image_url: string;
  category: string;
  subtitle: string;
  description: string;
  attributes: Record<string, unknown>;
  price_cents: number;
}

export interface StockSnapshot {
  in_stock: boolean;
}

export interface ProductPage {
  product: ProductDetail;
  stock: StockSnapshot;
}

// 订单行（storefront/orders、/orders）
export interface OrderItem {
  item_id: string;
  title: string;
  qty: number;
  refunded_qty?: number;
  sku?: string;
}

export interface Order {
  id: string;
  order_no: string;
  status: string;
  amount_cents: number;
  user_id?: string;
  payment_id?: string | null;
  items: OrderItem[];
}

// 售后工单（aftersales/tickets）
export interface TicketRecommendation {
  recommendation: string;
  confidence: number;
  rationale: string;
}

export interface Ticket {
  id: string;
  ticket_no: string;
  status: string;
  amount_cents: number;
  order_id?: string;
  reason?: string;
  description?: string;
  decision_note?: string;
  risk_score?: number;
  risk_reasons?: string[];
  agent_recommendation?: TicketRecommendation;
}

// 履约单（fulfillment/shipments）
export interface ShipmentItem {
  order_item_id: string;
  sku: string;
  qty: number;
}

export interface Shipment {
  id: string;
  order_id: string;
  status: string;
  sla_flagged: boolean;
  deadline_at: string;
  tracking_no: string | null;
  items: ShipmentItem[];
}

// 库存行（inventory/stocks）
export interface InventoryRow {
  sku: string;
  total: number;
  reserved: number;
  available: number;
}

// 订单状态轨迹（orders/:id/timeline）
export interface TimelineEvent {
  from: string;
  to: string;
  event: string;
  actor: string;
  at: string;
}

export interface Timeline {
  status_label: string;
  events: TimelineEvent[];
}

// Agent 列表项（/agents）
export interface AgentInfo {
  name: string;
  display_name?: string;
  description: string;
  trust: string;
  allowed_tools: string[];
}

// 运行证据卡（/agents/runs/:id/evidence）
export interface Evidence {
  run_id: string;
  skill: string;
  trust: string;
  status: string;
  tool_calls: unknown;
  denied_calls: unknown;
}

// 待审批运行（/agents/runs/pending）：参数已在服务端脱敏
export interface PendingRun {
  run_id: string;
  skill: string;
  user_id: string;
  tool: string;
  reason: string;
  args_preview: Record<string, unknown>;
  delegated: boolean;
  expires_in_seconds: number;
}

// 下单请求体（storefront/checkout）
export interface CheckoutPayload {
  items: { sku: string; qty: number }[];
  address: Record<string, string>;
}

// 售后建单请求体（aftersales/tickets）
export interface CreateTicketPayload {
  order_id: string;
  reason: string;
  description: string;
  type: string;
  order_item_id?: string;
  quantity?: number;
}

// Agent SSE 流式事件的 trace 步骤（渲染运行证据用）
export interface TraceStep {
  type: string;
  tool?: string;
  status?: string;
  duration_ms?: number;
}

export interface AgentRouteInfo {
  agent_ids: string[];
  confidence: number;
  reason: string;
  source: "llm" | "rules" | "fallback";
}

export interface AgentRunSummary {
  agent: string;
  run_id: string;
  status: string;
  answer: string;
  evidence?: Record<string, unknown>;
}

// 根运行与子 Agent 的终态；degraded 表示已降级但仍有可用结果，不能等同 completed。
export type AgentRunStatus =
  | "completed"
  | "degraded"
  | "failed"
  | "pending_approval"
  | "budget_exceeded"
  | "quota_exceeded"
  | "blocked"
  | "max_steps_exceeded";

export type ActiveAgentRunStatus = AgentRunStatus | "running";

export type AgentEvent =
  | (AgentRouteInfo & { type: "route" })
  | { type: "agent.start"; agent: string; run_id: string }
  | { type: "agent.end"; agent: string; run_id: string; status: string }
  | { type: "fallback"; reason: string };

export interface StreamFinal {
  run_id: string;
  answer: string;
  // 根运行终态独立于子 Agent 状态，前端不能从 completed 子运行推导根运行结果。
  status: AgentRunStatus;
  // 首轮新建或空闲拆分后返回的会话 id，前端必须续用，否则下一轮会再建一条会话
  conversation_id?: string | null;
  context?: ContextUsage | null;
  route?: AgentRouteInfo;
  agent_runs?: AgentRunSummary[];
}

// 会话上下文占用：后端按 token 粗估，前端据此在 80% 时提示开启新会话。
export interface ContextUsage {
  used_tokens: number;
  limit: number;
  ratio: number;
  stage: "ok" | "warn" | "split" | "hard";
  reserve_tokens: number;
  omitted_messages: number;
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

// 后端错误体约定 { code, message }；code 用于区分 MFA_REQUIRED 等可恢复状态
export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

// 登录只提交手机号与密码（MFA 时附带动态码）；角色永远由服务端按 DB 下发，前端不传角色
export async function login(phone: string, password: string, otp?: string): Promise<Session> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone, password, ...(otp ? { otp } : {}) }),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { code?: string; message?: string };
    throw new ApiError(body.code || "UNKNOWN", body.message || `登录失败 (${response.status})`);
  }
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

export interface MfaStatus {
  enabled: boolean;
}

export interface MfaEnroll {
  otpauth_url: string;
  secret: string;
}

// 管理员 MFA：绑定/确认/关闭/状态查询，均需 admin 身份，走统一 request 注入 Bearer token
export function getMfaStatus(): Promise<MfaStatus> {
  return request<MfaStatus>("/auth/mfa");
}

export function enrollMfa(): Promise<MfaEnroll> {
  return request<MfaEnroll>("/auth/mfa/enroll", { method: "POST" });
}

export function confirmMfa(code: string): Promise<MfaStatus> {
  return request<MfaStatus>("/auth/mfa/confirm", { method: "POST", body: JSON.stringify({ code }) });
}

export function disableMfa(password: string): Promise<MfaStatus> {
  return request<MfaStatus>("/auth/mfa/disable", { method: "POST", body: JSON.stringify({ password }) });
}

// 双提交 CSRF：读非 HttpOnly 的 shop_csrf Cookie，刷新/登出时回放到 Header
export function csrfToken(): string | null {
  const match = document.cookie.match(/(?:^|;\s*)shop_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

let refreshPromise: Promise<Session | null> | null = null;

export function refreshSession(): Promise<Session | null> {
  // 并发调用共享同一次刷新：refresh token 轮换会让第二次请求被判重放，
  // 若各发各的会 401 并把刚恢复的会话清掉。
  refreshPromise ??= doRefresh().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

async function doRefresh(): Promise<Session | null> {
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

// 刷新页面后 access token 只存在内存会丢失，但本地 session 还在。
// 此时先静默换一次 token，否则运营接口会以匿名身份返回 403，页面只显示空列表。
async function ensureAccessToken(): Promise<void> {
  if (accessToken || !getSession()) return;
  await refreshSession();
}

/**
 * 全站唯一的 HTTP 出口：统一拼 /api 前缀、注入登录态、解析错误响应。
 * 页面组件不得自行 fetch，避免路径与鉴权头散落各处。
 */
async function request<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
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
    return request<{ items: FeedItem[] }>(`/storefront/feed${query ? `?${query}` : ""}`);
  },
  product: (productId: string) => request<ProductPage>(`/storefront/products/${productId}`),
  // 下单必须带 Idempotency-Key，由调用方生成，保证重复提交不会产生重复订单。
  checkout: (payload: CheckoutPayload, idempotencyKey: string) =>
    request("/storefront/checkout", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload),
    }),
  orders: (status?: string) => request<{ items: Order[] }>(`/storefront/orders${status ? `?status=${status}` : ""}`),
  // 支付由独立 payment 服务处理：前端只拿 payment_id 调 mock 收银台确认
  pay: (paymentId: string) =>
    request(`/storefront/payments/${paymentId}/pay`, { method: "POST" }),
  timeline: (orderId: string) => request<Timeline>(`/orders/${orderId}/timeline`),
  inventory: () => request<{ items: InventoryRow[] }>("/inventory/stocks"),
  setStock: (sku: string, total: number) =>
    request("/inventory/stocktake", { method: "POST", body: JSON.stringify({ sku, total }) }),
  shipments: (status?: string) => request<{ items: Shipment[] }>(`/fulfillment/shipments${status ? `?status=${status}` : ""}`),
  ship: (shipmentId: string, items?: { order_item_id: string; qty: number }[]) =>
    request(`/fulfillment/shipments/${shipmentId}/ship`, {
      method: "POST",
      body: JSON.stringify({ carrier: "SF", ...(items ? { items } : {}) }),
    }),
  deliver: (shipmentId: string) =>
    request(`/fulfillment/shipments/${shipmentId}/deliver`, { method: "POST", body: "{}" }),
  tickets: (status?: string) => request<{ items: Ticket[] }>(`/aftersales/tickets${status ? `?status=${status}` : ""}`),
  createTicket: (payload: CreateTicketPayload) =>
    request("/aftersales/tickets", { method: "POST", body: JSON.stringify(payload) }),
  approveTicket: (ticketId: string) =>
    request(`/aftersales/tickets/${ticketId}/approve`, { method: "POST", body: JSON.stringify({ note: "人工审核通过" }) }),
  rejectTicket: (ticketId: string) =>
    request(`/aftersales/tickets/${ticketId}/reject`, { method: "POST", body: JSON.stringify({ note: "人工驳回" }) }),
  agents: () => request<{ items: AgentInfo[] }>("/agents"),
  policies: () => request("/policies"),
  evidence: (runId: string) => request<Evidence>(`/agents/runs/${runId}/evidence`),
  // 待审批的高风险工具运行：列表只给脱敏摘要，批准后才真正执行。
  pendingRuns: () => request<{ items: PendingRun[] }>("/agents/runs/pending"),
  approveRun: (runId: string) =>
    request(`/agents/runs/${runId}/approve`, { method: "POST", body: "{}" }),
  opsOrders: (status?: string) => request<{ items: Order[] }>(`/orders${status ? `?status=${status}` : ""}`),
  memory: (userId: string) => request(`/agents/memory/${userId}`),
  // 审计日志只对 admin 开放，服务端再校验一次；筛选字段可空，空值不拼进查询串
  listAuditLogs: (params: { action?: string; target_type?: string; target_id?: string; actor_id?: string; page?: number; size?: number } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== "")
        .map(([key, value]) => [key, String(value)]),
    ).toString();
    return request<AuditLogPage>(`/audit/logs${query ? `?${query}` : ""}`);
  },
  // 拒付列表：状态过滤 + 分页，空值不拼进查询串；处置按争议单号调 payment 服务
  chargebacks: (params: { status?: string; page?: number; size?: number } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== "")
        .map(([key, value]) => [key, String(value)]),
    ).toString();
    return request<ChargebackPage>(`/payments/chargebacks${query ? `?${query}` : ""}`);
  },
  resolveChargeback: (chargebackId: string, outcome: "won" | "lost") =>
    request(`/payments/chargebacks/${chargebackId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ outcome }),
    }),
  // 模型提供商预设
  providerPresets: () => request<{ items: ProviderPreset[] }>("/providers/presets"),
  // 模型提供商列表
  listProviders: () => request<{ items: ModelProvider[] }>("/providers"),
  // 创建模型提供商
  createProvider: (payload: {
    provider_type: string;
    name: string;
    base_url: string;
    model: string;
    api_key: string;
    shared?: boolean;
    is_default?: boolean;
  }) => request<ModelProvider>("/providers", { method: "POST", body: JSON.stringify(payload) }),
  // 更新模型提供商
  updateProvider: (
    providerId: string,
    payload: {
      name?: string;
      model?: string;
      base_url?: string;
      api_key?: string;
      is_default?: boolean;
      enabled?: boolean;
    },
  ) =>
    request<ModelProvider>(`/providers/${providerId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  // 删除模型提供商
  deleteProvider: (providerId: string) =>
    request<{ deleted: boolean }>(`/providers/${providerId}`, { method: "DELETE" }),
  // 测试模型提供商连通性
  testProvider: (providerId: string) =>
    request<{ ok: boolean; latency_ms: number; message: string }>(`/providers/${providerId}/test`, {
      method: "POST",
      body: "{}",
    }),
  // 会话列表
  listConversations: (params: { skill?: string; page?: number; size?: number } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== "")
        .map(([key, value]) => [key, String(value)]),
    ).toString();
    return request<ConversationPage>(`/conversations${query ? `?${query}` : ""}`);
  },
  // 会话消息列表（默认最新一页，cursor 用于加载更早历史）
  listMessages: (conversationId: string, params: { limit?: number; cursor?: string } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== "")
        .map(([key, value]) => [key, String(value)]),
    ).toString();
    return request<MessagePage>(`/conversations/${conversationId}/messages${query ? `?${query}` : ""}`);
  },
  // 拆分会话：旧会话冻结，新会话携带交接摘要继续
  splitConversation: (conversationId: string) =>
    request<{ conversation: Conversation; handoff: string }>(`/conversations/${conversationId}/split`, {
      method: "POST",
      body: "{}",
    }),
  // 归档较早消息，仅保留最近 keep_recent 条原文
  archiveConversation: (conversationId: string, keepRecent = 200) =>
    request<{ archived: number; keep_recent: number }>(
      `/conversations/${conversationId}/archive?keep_recent=${keepRecent}`,
      { method: "POST", body: "{}" },
    ),
  // 用量统计
  listUsage: (params: { group_by?: string; from?: string; to?: string; page?: number; size?: number } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== "")
        .map(([key, value]) => [key, String(value)]),
    ).toString();
    return request<UsagePage>(`/usage${query ? `?${query}` : ""}`);
  },
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

export type StreamEvent =
  | { type: "start"; skill: string; trust: string }
  | { type: "context"; context: ContextUsage }
  | { type: "trace"; event: TraceStep }
  | { type: "agent"; event: AgentEvent }
  | { type: "delta"; text: string }
  | { type: "final"; result: StreamFinal }
  | { type: "error"; message: string };

/**
 * Agent 对话走 SSE 流式返回：逐帧解析 data: 行并回调页面。
 * 不能用 response.json()，否则会等到整段结束时才更新 UI。
 * 新增支持 conversationId 和 providerId 参数。
 */
export async function chatStream(
  skill: string,
  message: string,
  sessionId: string,
  onEvent: (event: StreamEvent) => void,
  conversationId?: string,
  providerId?: string,
): Promise<void> {
  // 流式接口同样要保证 token 已恢复,否则刷新页面后首条消息会以匿名身份被拒
  await ensureAccessToken();
  const response = await fetch(`/api/agents/${skill}/chat`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      conversation_id: conversationId,
      provider_id: providerId,
    }),
  });
  if (!response.body) throw new Error("Agent 连接失败");
  // 非 2xx 时尝试解析后端错误体，把真实 message 透出给用户
  if (!response.ok) {
    const text = await response.text();
    let msg = `Agent 请求失败 (${response.status})`;
    try {
      const body = JSON.parse(text) as { message?: string; code?: string };
      if (body.message) msg = body.message;
    } catch {
      /* 纯文本响应直接截取 */
      if (text) msg = text.slice(0, 200);
    }
    throw new Error(msg);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // 按空行切分 SSE 事件；最后一段可能不完整,留在 buffer 等下一帧再拼。
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
