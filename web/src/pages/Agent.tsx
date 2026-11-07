import { useEffect, useRef, useState } from "react";
import { chatStream, api, getSession, type StreamEvent } from "../lib/api";

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
  const [agents, setAgents] = useState<any[]>([]);
  const [skill, setSkill] = useState("customer-service");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [trace, setTrace] = useState<StreamEvent[]>([]);
  const [runId, setRunId] = useState("");
  const [evidence, setEvidence] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // 流式答案的累积文本：逐 delta 追加，final 落库后清空，渲染成打字机效果。
  const [streaming, setStreaming] = useState("");
  // 会话 id 在页面生命周期内固定，让多轮对话共享 Agent 的上下文与记忆。
  const sessionId = useRef(`web-${crypto.randomUUID().slice(0, 8)}`);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.agents().then((data) => setAgents(data.items || [])).catch((exc) => setError(exc.message));
  }, []);

  useEffect(() => {
    // 新消息、等待中或流式累积中自动滚到底部，保证流式输出始终可见。
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy, streaming]);

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
        if (event.type === "delta") setStreaming((prev) => prev + event.text);
        if (event.type === "final") {
          const result = event.result;
          setRunId(result.run_id);
          setMessages((prev) => [...prev, { role: "assistant", text: result.answer }]);
          setStreaming("");
        }
        if (event.type === "error") setError(event.message);
      });
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
            onClick={() => { setSkill(agent.name); setMessages([]); setTrace([]); setEvidence(null); setStreaming(""); }}
          >
            <strong>{agent.name}</strong>
            <span>{agent.description}</span>
            <small>信任等级 {agent.trust} · 工具 {agent.allowed_tools.length}</small>
          </button>
        ))}
        {!session && <p className="hint">当前为访客身份，部分工具会被权限上限拦截。</p>}
      </aside>

      <section className="agent-main">
        <div className="chat-window">
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
