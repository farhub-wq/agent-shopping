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
  const sessionId = useRef(`web-${crypto.randomUUID().slice(0, 8)}`);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.agents().then((data) => setAgents(data.items || [])).catch((exc) => setError(exc.message));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  const send = async (text: string) => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError("");
    setMessages((prev) => [...prev, { role: "user", text }]);
    setInput("");
    setTrace([]);
    setEvidence(null);
    try {
      await chatStream(skill, text, sessionId.current, (event) => {
        if (event.type === "trace") setTrace((prev) => [...prev, event.event]);
        if (event.type === "final") {
          const result = event.result;
          setRunId(result.run_id);
          setMessages((prev) => [...prev, { role: "assistant", text: result.answer }]);
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
            onClick={() => { setSkill(agent.name); setMessages([]); setTrace([]); setEvidence(null); }}
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
          {busy && <div className="bubble assistant"><p className="muted">思考中…</p></div>}
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
