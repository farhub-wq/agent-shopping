import { useEffect, useRef, useState } from "react";
import { chatStream, api, getSession, type StreamEvent, type TraceStep, type AgentInfo, type Evidence, type ContextUsage, ModelProvider, Conversation } from "../api";

interface Message {
  role: "user" | "assistant";
  text: string;
}

const QUICK_PROMPTS: Record<string, string[]> = {
  "customer-service": ["SKU-1001 还有货吗？", "帮我看看订单 SO 的状态", "耳机推荐一款"],
  "ops-catalog": ["检查 SKU-1002 的合规性", "帮 SKU-1003 写一版卖点文案", "用沙箱算一下库存占比"],
  "ops-aftersales": ["看看待审核的售后工单", "这笔售后风险高吗？"],
};

export default function Agent() {
  const session = getSession();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [skill, setSkill] = useState("customer-service");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [trace, setTrace] = useState<TraceStep[]>([]);
  const [runId, setRunId] = useState("");
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
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
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.agents().then((data) => setAgents(data.items || [])).catch((exc) => setError(exc.message));
    // 加载模型提供商列表和会话历史（仅登录用户）
    if (session) {
      api.listProviders().then((data) => setProviders(data.items || [])).catch((exc) => setError(exc.message));
      loadConversations();
    }
  }, []);

  async function loadConversations() {
    try {
      const data = await api.listConversations({ skill, page: 1, size: 20 });
      setConversations(data.items || []);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  async function loadConversationMessages(convId: string) {
    try {
      const data = await api.listMessages(convId);
      // 将历史消息恢复到对话框（默认最新一页，更早历史用游标继续加载）
      setMessages(data.items.map(msg => ({ role: msg.role as "user" | "assistant", text: msg.content })));
      setMessageCursor(data.next_cursor);
      setConversationId(convId);
      // 旧会话的占用要等下一轮运行才能算出，先清空避免展示过期提示。
      setContextUsage(null);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  async function loadOlderMessages() {
    if (!conversationId || !messageCursor) return;
    try {
      const data = await api.listMessages(conversationId, { cursor: messageCursor });
      const older = data.items.map(msg => ({ role: msg.role as "user" | "assistant", text: msg.content }));
      setMessages(prev => [...older, ...prev]);
      setMessageCursor(data.next_cursor);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  function startNewSession() {
    // 新窗口 = 新 Redis 会话 + 新数据库会话：旧历史仍可回看，但不再占用本轮上下文。
    sessionId.current = `web-${crypto.randomUUID().slice(0, 8)}`;
    setConversationId(undefined);
    setMessages([]);
    setMessageCursor(null);
    setTrace([]);
    setEvidence(null);
    setStreaming("");
    setRunId("");
    setContextUsage(null);
    setError("");
  }

  async function splitConversation() {
    // 拆分后旧会话冻结，新会话带交接摘要继续，前端切到新会话 id。
    if (!conversationId) return;
    try {
      const data = await api.splitConversation(conversationId);
      setConversationId(data.conversation.id);
      setMessages([]);
      setMessageCursor(null);
      setTrace([]);
      setEvidence(null);
      setStreaming("");
      setRunId("");
      setContextUsage(null);
      await loadConversations();
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  useEffect(() => {
    // 新消息、等待中或流式累积中自动滚到底部，保证流式输出始终可见。
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy, streaming]);

  const activeConversation = conversations.find((item) => item.id === conversationId);
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
    if (!text.trim() || busy) return;
    setBusy(true);
    setError("");
    setMessages((prev) => [...prev, { role: "user", text }]);
    setInput("");
    setTrace([]);
    setEvidence(null);
    setStreaming("");
    try {
      await chatStream(skill, text, sessionId.current, (event) => {
        // trace 事件逐条累积，形成工具调用证据；delta 逐片累积成流式答案；
        // final 才把完整答案落进消息列表并记录 run_id。
        if (event.type === "trace") setTrace((prev) => [...prev, event.event]);
        if (event.type === "context") setContextUsage(event.context);
        if (event.type === "delta") setStreaming((prev) => prev + event.text);
        if (event.type === "final") {
          const result = event.result;
          setRunId(result.run_id);
          setMessages((prev) => [...prev, { role: "assistant", text: result.answer }]);
          setStreaming("");
          if (result.context) setContextUsage(result.context);
          // 首轮新建/空闲拆分会返回新会话 id，后续消息必须续用同一条会话。
          if (result.conversation_id) {
            if (result.conversation_id !== conversationId) setMessageCursor(null);
            setConversationId(result.conversation_id);
          }
          // 刷新会话列表
          if (session) loadConversations();
        }
        if (event.type === "error") setError(event.message);
      }, conversationId, providerId);
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const showEvidence = async () => {
    if (!runId) return;
    try {
      // 证据卡来自本次 run 的 trace 记录，用于复核 Agent 是否越权或失败。
      setEvidence(await api.evidence(runId));
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const currentAgent = agents.find((item) => item.name === skill);

  return (
    <div className="agent-layout">
      <aside className="panel agent-side">
        <h2>智能体</h2>
        {agents.map((agent) => (
          <button
            key={agent.name}
            className={agent.name === skill ? "agent-item active" : "agent-item"}
            onClick={() => { setSkill(agent.name); setMessages([]); setTrace([]); setEvidence(null); setStreaming(""); setContextUsage(null); }}
          >
            <strong>{agent.name}</strong>
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
                <button className="ghost small" onClick={splitConversation}>
                  拆分会话继续
                </button>
              )}
              <button className="ghost small" onClick={startNewSession}>开启新会话</button>
            </div>
          </div>
        )}

        <div className="chat-window">
          {messageCursor && (
            <button className="ghost small" onClick={loadOlderMessages}>加载更早消息</button>
          )}
          {messages.length === 0 && (
            <div className="chat-empty">
              <h2>{currentAgent?.name || "智能助手"}</h2>
              <p className="muted">{currentAgent?.description || "选择左侧智能体开始对话"}</p>
              <div className="chips">
                {(QUICK_PROMPTS[skill] || []).map((prompt) => (
                  <button key={prompt} className="chip" onClick={() => send(prompt)}>{prompt}</button>
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

        {error && <p className="error">{error}</p>}

        <div className="composer">
          <textarea
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
          <button className="primary" disabled={busy} onClick={() => send(input)}>发送</button>
        </div>

        {(trace.length > 0 || evidence) && (
          <div className="panel trace-panel">
            <div className="panel-head">
              <h2>运行证据</h2>
              {runId && <button className="ghost small" onClick={showEvidence}>查看证据卡</button>}
            </div>
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
