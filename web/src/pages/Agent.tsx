import { useEffect, useRef, useState } from "react";
import {
  chatStream,
  api,
  getSession,
  type TraceStep,
  type AgentInfo,
  type AgentRouteInfo,
  type AgentRunSummary,
  type AgentRunStatus,
  type ActiveAgentRunStatus,
  type Evidence,
  type ContextUsage,
  type ModelProvider,
  type Conversation,
} from "../api";

interface Message {
  role: "user" | "assistant";
  text: string;
}

interface ActiveAgentRun {
  agent: string;
  run_id: string;
  status: ActiveAgentRunStatus;
}

const BUILTIN_AGENT_NAMES: Record<string, string> = {
  auto: "自动分派",
  chitchat: "闲聊助手",
  presales: "售前顾问",
  inventory: "库存助手",
  aftersales: "售后助手",
  "customer-service": "综合客服",
};

const AGENT_RUN_STATUS_LABELS: Record<AgentRunStatus, string> = {
  completed: "已完成",
  degraded: "已降级",
  failed: "失败",
  pending_approval: "待审批",
  budget_exceeded: "超出预算",
  quota_exceeded: "配额不足",
  blocked: "已拦截",
  max_steps_exceeded: "达到最大推理步数",
};

const ACTIVE_AGENT_STATUS_LABELS: Record<ActiveAgentRunStatus, string> = {
  running: "执行中",
  ...AGENT_RUN_STATUS_LABELS,
};

const QUICK_PROMPTS: Record<string, string[]> = {
  auto: ["你好，今天有什么可以帮你？", "推荐一款耳机", "SKU-1001 还有货吗？", "我的快递到哪了", "我的售后审核到哪了", "查一下 SKU-1001 库存，再推荐一款耳机"],
  "customer-service": ["SKU-1001 还有货吗？", "帮我查订单 SO 的物流", "我的售后进度", "耳机推荐一款"],
  "aftersales": ["我的订单在哪里", "我的快递到哪了", "我的售后审核到哪了", "想申请退货"],
  "ops-catalog": ["检查 SKU-1002 的合规性", "帮 SKU-1003 写一版卖点文案", "用沙箱算一下库存占比"],
  "ops-aftersales": ["看看待审核的售后工单", "这笔售后风险高吗？"],
};

export default function Agent() {
  const session = getSession();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  // 只有服务端确认当前技能可见后，才允许发起对话。
  const [agentsReady, setAgentsReady] = useState(false);
  const [skill, setSkill] = useState("auto");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [trace, setTrace] = useState<TraceStep[]>([]);
  const [runId, setRunId] = useState("");
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [routeLabel, setRouteLabel] = useState("");
  const [route, setRoute] = useState<AgentRouteInfo | null>(null);
  const [activeAgents, setActiveAgents] = useState<ActiveAgentRun[]>([]);
  const [agentRuns, setAgentRuns] = useState<AgentRunSummary[]>([]);
  const [rootStatus, setRootStatus] = useState<AgentRunStatus | "">("");
  const [fallbackReason, setFallbackReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [splitPending, setSplitPending] = useState(false);
  // 流式答案的累积文本：逐 delta 追加，final 落库后清空，渲染成打字机效果。
  const [streaming, setStreaming] = useState("");
  // 模型提供商列表与当前选中的 provider
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [providerId, setProviderId] = useState<string | undefined>(undefined);
  // 会话列表与当前会话
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>(undefined);
  // 当前会话的上下文占用；达到 80% 时提示换新窗口，避免长对话质量下降。
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null);
  // 更早历史的分页游标：为空表示当前会话已加载到最早一条。
  const [messageCursor, setMessageCursor] = useState<string | null>(null);
  // 会话 id 在页面生命周期内固定，让多轮对话共享 Agent 的上下文与记忆。
  const sessionId = useRef(`web-${crypto.randomUUID().slice(0, 8)}`);
  // 流式过程中切换专家时，final 仍应刷新当前专家对应的会话列表。
  const skillRef = useRef(skill);
  const agentsReadyRef = useRef(false);
  const busyRef = useRef(false);
  const splitPendingRef = useRef(false);
  const listRequestRef = useRef(0);
  const historyRequestRef = useRef(0);
  const evidenceRequestRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);

  // 切换轮次或会话时先作废在途证据请求，再清掉旧运行的展示状态。
  function clearRunDisplay() {
    evidenceRequestRef.current += 1;
    setTrace([]);
    setEvidence(null);
    setStreaming("");
    setRunId("");
    setRouteLabel("");
    setRoute(null);
    setActiveAgents([]);
    setAgentRuns([]);
    setRootStatus("");
    setFallbackReason("");
  }

  function agentName(agentId: string): string {
    const agent = agents.find((item) => item.name === agentId);
    return agent?.display_name || BUILTIN_AGENT_NAMES[agentId] || agent?.name || "专家";
  }

  function formatRouteLabel(routeInfo: AgentRouteInfo): string {
    const names = routeInfo.agent_ids.map(agentName).join("＋");
    if (routeInfo.source === "fallback") return names ? `已回退至${names}` : "";
    return names ? `已识别：${names}` : "";
  }

  function terminalAgentStatus(status: string): ActiveAgentRunStatus {
    if (
      status === "completed" || status === "degraded" || status === "failed" ||
      status === "pending_approval" || status === "budget_exceeded" ||
      status === "quota_exceeded" || status === "blocked" ||
      status === "max_steps_exceeded"
    ) return status;
    return "failed";
  }

  useEffect(() => {
    api.agents().then((data) => {
      const visibleAgents = data.items || [];
      if (visibleAgents.length === 0) throw new Error("没有可用的智能体");
      setAgents(visibleAgents);
      // auto 仅对服务端可见列表包含它的身份开放；不可用时改选首个可见技能。
      if (!visibleAgents.some((agent) => agent.name === skillRef.current)) {
        skillRef.current = visibleAgents[0].name;
        setSkill(skillRef.current);
      }
      agentsReadyRef.current = true;
      setAgentsReady(true);
      if (session) void loadConversations(skillRef.current);
    }).catch((exc) => {
      agentsReadyRef.current = false;
      setAgentsReady(false);
      setError((exc as Error).message);
    });
    // 加载模型提供商列表（仅登录用户）
    if (session) {
      api.listProviders().then((data) => setProviders(data.items || [])).catch((exc) => setError((exc as Error).message));
    }
  }, []);

  async function loadConversations(selectedSkill = skill) {
    const requestId = ++listRequestRef.current;
    try {
      const data = await api.listConversations({ skill: selectedSkill, page: 1, size: 20 });
      // 快速切换或重复加载时只接受最新请求，避免旧列表覆盖当前选择。
      if (requestId === listRequestRef.current && selectedSkill === skillRef.current) {
        setConversations(data.items || []);
      }
    } catch (exc) {
      if (requestId === listRequestRef.current && selectedSkill === skillRef.current) {
        setError((exc as Error).message);
      }
    }
  }

  function changeSkill(nextSkill: string) {
    if (nextSkill === skill || splitPendingRef.current) return;
    skillRef.current = nextSkill;
    setSkill(nextSkill);
    setConversations([]);
    historyRequestRef.current += 1;
    // 只切换请求入口和会话列表，当前对话及运行证据继续保留。
    void loadConversations(nextSkill);
  }

  async function loadConversationMessages(convId: string) {
    if (busyRef.current || splitPendingRef.current) return;
    const requestId = ++historyRequestRef.current;
    clearRunDisplay();
    try {
      const data = await api.listMessages(convId);
      if (requestId !== historyRequestRef.current || busyRef.current) return;
      // 将历史消息恢复到对话框（默认最新一页，更早历史用游标继续加载）
      setMessages(data.items.map(msg => ({ role: msg.role as "user" | "assistant", text: msg.content })));
      setMessageCursor(data.next_cursor);
      setConversationId(convId);
      // 旧会话的占用要等下一轮运行才能算出，先清空避免展示过期提示。
      setContextUsage(null);
    } catch (exc) {
      if (requestId === historyRequestRef.current && !busyRef.current) setError((exc as Error).message);
    }
  }

  async function loadOlderMessages() {
    if (!conversationId || !messageCursor || busyRef.current || splitPendingRef.current) return;
    const requestId = ++historyRequestRef.current;
    const selectedConversationId = conversationId;
    try {
      const data = await api.listMessages(selectedConversationId, { cursor: messageCursor });
      if (requestId !== historyRequestRef.current || selectedConversationId !== conversationId || busyRef.current) return;
      const older = data.items.map(msg => ({ role: msg.role as "user" | "assistant", text: msg.content }));
      setMessages(prev => [...older, ...prev]);
      setMessageCursor(data.next_cursor);
    } catch (exc) {
      if (requestId === historyRequestRef.current && !busyRef.current) setError((exc as Error).message);
    }
  }

  function startNewSession() {
    if (busyRef.current || splitPendingRef.current) return;
    historyRequestRef.current += 1;
    // 新窗口 = 新 Redis 会话 + 新数据库会话：旧历史仍可回看，但不再占用本轮上下文。
    sessionId.current = `web-${crypto.randomUUID().slice(0, 8)}`;
    setConversationId(undefined);
    setMessages([]);
    setMessageCursor(null);
    clearRunDisplay();
    setContextUsage(null);
    setError("");
  }

  async function splitConversation() {
    // 拆分后旧会话冻结，新会话带交接摘要继续，前端切到新会话 id。
    if (!conversationId || busyRef.current || splitPendingRef.current) return;
    splitPendingRef.current = true;
    setSplitPending(true);
    const requestId = ++historyRequestRef.current;
    try {
      const data = await api.splitConversation(conversationId);
      if (requestId !== historyRequestRef.current || busyRef.current) return;
      clearRunDisplay();
      setConversationId(data.conversation.id);
      setMessages([]);
      setMessageCursor(null);
      setContextUsage(null);
      await loadConversations();
    } catch (exc) {
      if (requestId === historyRequestRef.current && !busyRef.current) setError((exc as Error).message);
    } finally {
      splitPendingRef.current = false;
      setSplitPending(false);
    }
  }

  useEffect(() => {
    // 新消息、等待中或流式累积中自动滚到底部，保证流式输出始终可见。
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy, streaming]);

  const activeConversation = conversations.find((item) => item.id === conversationId);
  const controlsBusy = busy || splitPending;
  // 累计 token 到拆分阈值或后端建议拆分时展示入口；仅提示，不静默切换会话。
  const splitSuggested = Boolean(
    conversationId &&
      (contextUsage?.stage === "split" ||
        contextUsage?.stage === "hard" ||
        activeConversation?.context_state === "split" ||
        activeConversation?.split_suggested === "context"),
  );
  const bannerStage =
    contextUsage && contextUsage.stage !== "ok"
      ? contextUsage.stage
      : activeConversation?.context_state === "split"
        ? "split"
        : null;

  const send = async (text: string) => {
    if (!text.trim() || busyRef.current || splitPendingRef.current || !agentsReadyRef.current) return;
    historyRequestRef.current += 1;
    busyRef.current = true;
    setBusy(true);
    setError("");
    setMessages((prev) => [...prev, { role: "user", text }]);
    setInput("");
    clearRunDisplay();
    try {
      await chatStream(skill, text, sessionId.current, (event) => {
        // trace 事件逐条累积，形成工具调用证据；delta 逐片累积成流式答案；
        // final 才把完整答案落进消息列表并记录 run_id。
        if (event.type === "trace") setTrace((prev) => [...prev, event.event]);
        if (event.type === "context") setContextUsage(event.context);
        if (event.type === "delta") setStreaming((prev) => prev + event.text);
        if (event.type === "agent") {
          const agentEvent = event.event;
          if (agentEvent.type === "route") {
            setRoute(agentEvent);
            setRouteLabel(formatRouteLabel(agentEvent));
            // fallback 原因独立保存，避免后续 route 事件覆盖已展示的降级信息。
            if (agentEvent.source === "fallback") {
              setFallbackReason((prev) => prev || agentEvent.reason);
            }
          }
          if (agentEvent.type === "agent.start") {
            setActiveAgents((prev) => [
              ...prev.filter((item) => item.run_id !== agentEvent.run_id),
              { agent: agentEvent.agent, run_id: agentEvent.run_id, status: "running" },
            ]);
          }
          if (agentEvent.type === "agent.end") {
            const status = terminalAgentStatus(agentEvent.status);
            setActiveAgents((prev) => prev.map((item) =>
              item.run_id === agentEvent.run_id ? { ...item, status } : item,
            ));
          }
          if (agentEvent.type === "fallback") setFallbackReason(agentEvent.reason);
        }
        if (event.type === "final") {
          const result = event.result;
          // 新根 run_id 绑定前再次作废旧证据请求，确保 A 的证据不会展示在 B 的 final 下。
          evidenceRequestRef.current += 1;
          setEvidence(null);
          setRunId(result.run_id);
          setRootStatus(result.status);
          setMessages((prev) => [...prev, { role: "assistant", text: result.answer }]);
          setStreaming("");
          if (result.context) setContextUsage(result.context);
          if (result.route) {
            setRoute(result.route);
            setRouteLabel(formatRouteLabel(result.route));
            if (result.route.source === "fallback") {
              setFallbackReason((prev) => prev || result.route?.reason || "");
            }
          }
          setAgentRuns(result.agent_runs || []);
          if (result.agent_runs) {
            setActiveAgents(result.agent_runs.map((item) => ({
              agent: item.agent,
              run_id: item.run_id,
              status: terminalAgentStatus(item.status),
            })));
          }
          // 首轮新建/空闲拆分会返回新会话 id，后续消息必须续用同一条会话。
          if (result.conversation_id) {
            if (result.conversation_id !== conversationId) setMessageCursor(null);
            setConversationId(result.conversation_id);
          }
          // 流式过程中允许切换专家，因此刷新列表时以最新选择为准。
          if (session) void loadConversations(skillRef.current);
        }
        if (event.type === "error") setError(event.message);
      }, conversationId, providerId);
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const showEvidence = async () => {
    if (!runId || busyRef.current || splitPendingRef.current) return;
    const selectedRunId = runId;
    const requestId = ++evidenceRequestRef.current;
    try {
      // 证据卡来自本次 run 的 trace 记录，用于复核 Agent 是否越权或失败。
      const data = await api.evidence(selectedRunId);
      if (requestId === evidenceRequestRef.current && data.run_id === selectedRunId) setEvidence(data);
    } catch (exc) {
      if (requestId === evidenceRequestRef.current) setError((exc as Error).message);
    }
  };

  const currentAgent = agents.find((item) => item.name === skill);
  const currentAgentName = skill === "auto"
    ? "自动分派"
    : currentAgent?.display_name || currentAgent?.name || "智能助手";
  const currentRouteLabel = routeLabel || (route ? formatRouteLabel(route) : "");

  return (
    <div className="agent-layout">
      <aside className="panel agent-side">
        <h2>智能体</h2>
        {agents.map((agent) => (
          <button
            key={agent.name}
            className={agent.name === skill ? "agent-item active" : "agent-item"}
            disabled={splitPending}
            onClick={() => changeSkill(agent.name)}
          >
            <strong>{agent.name === "auto" ? "自动分派" : agent.display_name || agent.name}</strong>
            <span>{agent.description}</span>
            <small>信任等级 {agent.trust} · 工具 {agent.allowed_tools.length}</small>
          </button>
        ))}
        {!session && <p className="hint">当前为访客身份，部分工具会被权限上限拦截。</p>}
      </aside>

      <section className="agent-main">
        {/* provider 选择器 */}
        {session && providers.length > 0 && (
          <div className="panel" style={{ padding: "0.75rem 1rem", marginBottom: "1rem" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
              <label style={{ fontSize: "0.875rem", fontWeight: 500 }}>选择模型:</label>
              <select
                style={{
                  flex: 1,
                  border: "1px solid #d1d5db",
                  borderRadius: "0.375rem",
                  padding: "0.5rem",
                  fontSize: "0.875rem",
                }}
                value={providerId || ""}
                onChange={(e) => setProviderId(e.target.value || undefined)}
              >
                <option value="">默认模型</option>
                {providers.filter(p => p.enabled).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.model})
                  </option>
                ))}
              </select>
            </div>
          </div>
        )}

        {/* 会话历史 */}
        {session && conversations.length > 0 && (
          <div className="panel" style={{ padding: "0.75rem 1rem", marginBottom: "1rem", maxHeight: "150px", overflow: "auto" }}>
            <h3 style={{ fontSize: "0.875rem", fontWeight: 600, marginBottom: "0.5rem" }}>历史会话</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
              {conversations.map((conv) => (
                <button
                  key={conv.id}
                  disabled={controlsBusy}
                  onClick={() => loadConversationMessages(conv.id)}
                  className={conv.id === conversationId ? "chip active" : "chip"}
                  style={{
                    textAlign: "left",
                    padding: "0.5rem",
                    fontSize: "0.8125rem",
                    border: conv.id === conversationId ? "1px solid #3b82f6" : "1px solid #e5e7eb",
                    borderRadius: "0.375rem",
                  }}
                >
                  {conv.title || conv.id.slice(0, 8)}
                  <small style={{ marginLeft: "0.5rem", color: "#9ca3af" }}>
                    {new Date(conv.last_message_at).toLocaleString()}
                  </small>
                </button>
              ))}
            </div>
          </div>
        )}

        {bannerStage && (
          <div className={`context-banner ${bannerStage}`} role="status" aria-live="polite">
            <span>
              {contextUsage
                ? `当前会话已占用 ${Math.min(100, Math.round(contextUsage.ratio * 100))}% 上下文（约 ${contextUsage.used_tokens}/${contextUsage.limit} tokens）${
                    contextUsage.omitted_messages > 0
                      ? `，已省略 ${contextUsage.omitted_messages} 条较早对话`
                      : ""
                  }`
                : "该会话已拆分或接近上限"}
              {bannerStage === "hard"
                ? "，继续对话可能失败，请立即拆分或开启新会话。"
                : "，建议拆分会话以保持回答质量。"}
            </span>
            <div style={{ display: "flex", gap: "0.5rem" }}>
              {splitSuggested && (
                <button className="ghost small" disabled={controlsBusy} onClick={splitConversation}>
                  拆分会话继续
                </button>
              )}
              <button className="ghost small" disabled={controlsBusy} onClick={startNewSession}>开启新会话</button>
            </div>
          </div>
        )}

        <div className="chat-window">
          {messageCursor && (
            <button className="ghost small" disabled={controlsBusy} onClick={loadOlderMessages}>加载更早消息</button>
          )}
          {messages.length === 0 && (
            <div className="chat-empty">
              <h2>{currentAgentName}</h2>
              <p className="muted">{currentAgent?.description || (skill === "auto" ? "自动识别问题并串行分派专家" : "选择左侧智能体开始对话")}</p>
              <div className="chips">
                {(QUICK_PROMPTS[skill] || []).map((prompt) => (
                  <button key={prompt} className="chip" disabled={!agentsReady || controlsBusy} onClick={() => send(prompt)}>{prompt}</button>
                ))}
              </div>
            </div>
          )}
          {messages.map((message, index) => (
            <div key={index} className={`bubble ${message.role}`}>
              <p>{message.text}</p>
            </div>
          ))}
          {busy && (
            <div className="bubble assistant">
              {/* 流式已到文本时用正常样式，空时显示占位思考提示。 */}
              <p className={streaming ? "" : "muted"}>{streaming || "思考中…"}</p>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {(currentRouteLabel || rootStatus || fallbackReason || activeAgents.length > 0) && (
          <div className="panel" role="status" aria-live="polite" style={{ padding: "0.75rem 1rem" }}>
            {currentRouteLabel && <p>{currentRouteLabel}</p>}
            {rootStatus && <p><strong>根运行：</strong>{AGENT_RUN_STATUS_LABELS[rootStatus]}</p>}
            {fallbackReason && <p className="hint">降级原因：{fallbackReason}</p>}
            {activeAgents.length > 0 && (
              <div className="chips">
                {activeAgents.map((item) => (
                  <span className="chip" key={item.run_id}>
                    <strong>{agentName(item.agent)}</strong>
                    <small style={{ marginLeft: "0.5rem" }}>{ACTIVE_AGENT_STATUS_LABELS[item.status]}</small>
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        {error && <p className="error">{error}</p>}

        <div className="composer">
          <textarea
            disabled={!agentsReady || controlsBusy}
            placeholder="描述你的问题，例如「SKU-1001 还有货吗」"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                send(input);
              }
            }}
          />
          <button className="primary" disabled={!agentsReady || controlsBusy} onClick={() => send(input)}>发送</button>
        </div>

        {(trace.length > 0 || evidence || agentRuns.length > 0) && (
          <div className="panel trace-panel">
            <div className="panel-head">
              <h2>运行证据</h2>
              {runId && <button className="ghost small" disabled={controlsBusy} onClick={showEvidence}>查看证据卡</button>}
            </div>
            {agentRuns.length > 0 && (
              <>
                <h3 style={{ fontSize: "0.875rem" }}>子 Agent 运行</h3>
                <ul className="trace-list">
                  {agentRuns.map((item) => (
                    <li key={item.run_id}>
                      <strong>{agentName(item.agent)}</strong>
                      <span className="tag">{item.status}</span>
                      <small>run_id: {item.run_id}</small>
                    </li>
                  ))}
                </ul>
              </>
            )}
            <ul className="trace-list">
              {trace.map((event, index) => (
                <li key={index}>
                  {/* trace 条目字段按工具调用类型可选，有值才渲染对应片段。 */}
                  <span className="tag">{event.type}</span>
                  {event.tool && <strong>{event.tool}</strong>}
                  {event.status && <span className={`status ${event.status}`}>{event.status}</span>}
                  {event.duration_ms !== undefined && <small>{event.duration_ms}ms</small>}
                </li>
              ))}
            </ul>
            {evidence && (
              <pre className="evidence">
{JSON.stringify(
  {
    run_id: evidence.run_id,
    skill: evidence.skill,
    trust: evidence.trust,
    status: evidence.status,
    tool_calls: evidence.tool_calls,
    denied_calls: evidence.denied_calls,
  },
  null,
  2,
)}
              </pre>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
