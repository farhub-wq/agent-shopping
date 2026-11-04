"""Agent 运行时：ReAct 循环 + 权限护栏 + 记忆注入 + 证据产出。

设计取舍：不做「无限制自治」的通用 Agent，而是把每次运行约束在
「一个技能、一组工具、一个步数上限、一个可审批边界」内。
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from shop_common.errors import ApiError
from shop_common.logging import get_logger

from .llm import LLMProvider
from .manifest import SkillManifest
from .memory import MemoryManager
from .tools import ToolContext, ToolRegistry
from .trace import TraceLedger

logger = get_logger(__name__)


@dataclass
class AgentRunResult:
    run_id: str
    skill: str
    answer: str
    status: str
    evidence: dict[str, Any]
    trace: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "skill": self.skill,
            "answer": self.answer,
            "status": self.status,
            "evidence": self.evidence,
            "pending": self.pending,
        }


class AgentRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        provider: LLMProvider,
        skills: dict[str, SkillManifest],
        memory: MemoryManager,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.skills = skills
        self.memory = memory

    def _build_system(self, manifest: SkillManifest, memories: list[dict[str, Any]]) -> str:
        # 把召回记忆与运行约束拼进 system prompt：
        # 记忆提供个性化上下文，约束则把「不许编造、不可逆只能提议」写死在每次运行里。
        context = "\n".join(f"- [{item['kind']}] {item['text']}" for item in memories) or "- 无"
        return (
            f"{manifest.system_prompt}\n\n"
            f"## 当前用户记忆\n{context}\n\n"
            "## 运行约束\n"
            f"- 信任等级：{manifest.trust}\n"
            f"- 允许工具：{', '.join(manifest.allowed_tools) or '无'}\n"
            "- 只能通过工具获取事实，禁止编造订单、库存、金额。\n"
            "- 涉及退款、改价、发布等不可逆动作时，只能提议，不能执行。\n"
            "- 工具报错时必须如实说明，不得把失败描述成成功。"
        )

    async def _prepare(
        self, skill_name: str, user_message: str, ctx: ToolContext
    ) -> tuple[SkillManifest, str, list[dict[str, Any]]]:
        manifest = self.skills.get(skill_name)
        if manifest is None:
            raise ApiError("NOT_FOUND", f"技能 {skill_name} 不存在")
        # 准入兜底：带 high finding 的技能在运行入口再次被拒，
        # 即使有人绕过 /agents 的展示判断直接调用 chat 也执行不了。
        if any(finding.severity == "high" for finding in manifest.findings):
            raise ApiError("FORBIDDEN", f"技能 {skill_name} 未通过准入检查")

        await self.memory.extract_semantic(ctx.user_id, user_message, ctx.tenant)
        memories = await self.memory.retrieve(ctx.user_id, user_message, k=5)
        system = self._build_system(manifest, memories)
        working = await self.memory.working_messages(ctx.session_id, limit=6)
        messages = [*working, {"role": "user", "content": user_message}]
        return manifest, system, messages

    async def run(
        self,
        skill_name: str,
        user_message: str,
        ctx: ToolContext,
        *,
        max_steps: int | None = None,
    ) -> AgentRunResult:
        ledger = TraceLedger(ctx.run_id)
        manifest, system, messages = await self._prepare(skill_name, user_message, ctx)
        # 工具白名单取 manifest 声明与注册表的交集：
        # 声明了但没注册的忽略，注册了但没声明的不会暴露给模型。
        allowed = [name for name in manifest.allowed_tools if self.registry.get(name)]
        specs = self.registry.specs(allowed)
        steps_limit = max_steps or manifest.max_steps
        usage = {"prompt_chars": len(system), "steps": 0, "tool_calls": 0, "estimated_tokens": 0}
        status = "completed"
        answer = ""
        pending: dict[str, Any] | None = None
        tool_errors = 0

        ledger.append("run.start", skill=skill_name, trust=manifest.trust, user=ctx.user_id, message=user_message)

        for step in range(steps_limit):
            usage["steps"] = step + 1
            # 每步都先落 agent.step 再执行动作：trace 顺序即真实决策顺序，
            # 出问题时能还原模型在拿到什么信息后做了什么。
            decision = await self.provider.decide(system, messages, specs)
            usage["prompt_chars"] += len(json.dumps(messages, ensure_ascii=False))
            ledger.append(
                "agent.step",
                step=step + 1,
                kind=decision.kind,
                tool=decision.tool or None,
                content=(decision.content or "")[:200],
            )
            if decision.kind == "final":
                answer = decision.content
                break

            usage["tool_calls"] += 1
            ledger.append("tool.call", tool=decision.tool, args=decision.args)
            result = await self.registry.execute(decision.tool, decision.args, ctx, trust=manifest.trust)
            ledger.append(
                "tool.result",
                tool=decision.tool,
                status=result.status,
                duration_ms=result.duration_ms,
                preview=str(result.data)[:200],
            )
            if result.status == "needs_approval":
                # 不可逆动作到此为止：不执行、不继续推理，转存待审批负载等人工处理。
                status = "pending_approval"
                pending = {"tool": decision.tool, "args": decision.args, "reason": result.message}
                ledger.append("approval.requested", tool=decision.tool, args=decision.args)
                break
            if result.status in {"error", "denied"}:
                # 工具失败不中断循环（模型还可换路），但整体标记 degraded，
                # 保证最终答案不会被当成完整成功。
                tool_errors += 1
                status = "degraded" if status == "completed" else status

            call_id = f"call_{uuid.uuid4().hex[:12]}"
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": decision.tool,
                                "arguments": json.dumps(decision.args, ensure_ascii=False),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result.to_dict(), ensure_ascii=False),
                }
            )
        else:
            # for-else：循环跑满步数仍未得到 final 决策，说明任务无法在预算内收敛。
            status = "max_steps_exceeded"
            answer = "达到最大推理步数，未能完成任务。请缩小问题范围或转人工处理。"

        if pending is not None:
            answer = "该操作需要人工审批，已生成待审批请求。"
            # 待审批负载只保留 1 小时：超时即失效，避免过期请求被无意批准。
            await self.memory.redis.set(
                f"shop:harness:pending:{ledger.run_id}",
                # approved 是 set，序列化成 list 才能放进 JSON；重放时再还原。
                json.dumps({"skill": skill_name, "ctx": ctx.__dict__ | {"approved": list(ctx.approved)}, **pending}, ensure_ascii=False),
                ex=3600,
            )

        if tool_errors and status == "completed":
            answer = f"注意：本次执行中有 {tool_errors} 次工具调用失败，以下结论可能不完整。\n{answer}"

        usage["estimated_tokens"] = usage["prompt_chars"] // 3

        # 会话结束统一落三处记忆：工作记忆保留原文、情节记忆保留结论、
        # 语义事实在 _prepare 阶段已抽取。失败状态给更高重要性，便于后续复盘。
        await self.memory.append_turn(ctx.session_id, "user", user_message)
        await self.memory.append_turn(ctx.session_id, "assistant", answer)
        await self.memory.remember(
            ctx.user_id,
            f"用户问题：{user_message[:80]}；处理结论：{answer[:120]}",
            kind="episodic",
            importance=0.45 if status == "completed" else 0.6,
            tenant=ctx.tenant,
            metadata={"skill": skill_name, "run_id": ledger.run_id, "status": status},
        )
        await self.memory.summarize_session(ctx.session_id, ctx.user_id)
        ledger.append("memory.write", user=ctx.user_id, skill=skill_name)
        ledger.append("run.end", status=status, answer=answer[:200])

        evidence = ledger.evidence_card(
            skill=skill_name,
            skill_hash=manifest.content_hash,
            trust=manifest.trust,
            model=self.provider.name,
            usage=usage,
            final_status=status,
        )
        return AgentRunResult(
            run_id=ledger.run_id,
            skill=skill_name,
            answer=answer,
            status=status,
            evidence=evidence,
            trace=ledger.events,
            pending=pending,
        )

    async def run_stream(
        self, skill_name: str, user_message: str, ctx: ToolContext
    ) -> AsyncIterator[dict[str, Any]]:
        """SSE 友好：先推步骤事件，最后推完整结果。"""
        manifest = self.skills.get(skill_name)
        if manifest is None:
            yield {"type": "error", "message": f"技能 {skill_name} 不存在"}
            return
        yield {"type": "start", "skill": skill_name, "trust": manifest.trust}
        result = await self.run(skill_name, user_message, ctx)
        # 只推关键事件给前端：run.start/run.end 已在 final 结果里，避免流式输出冗余。
        for event in result.trace:
            if event["type"] in {"agent.step", "tool.call", "tool.result", "approval.requested"}:
                yield {"type": "trace", "event": event}
        yield {"type": "final", "result": result.to_dict()}

    async def approve_and_replay(self, run_id: str, ctx: ToolContext) -> AgentRunResult:
        assert self.memory.redis is not None
        raw = await self.memory.redis.get(f"shop:harness:pending:{run_id}")
        if not raw:
            raise ApiError("NOT_FOUND", "待审批请求不存在或已过期")
        payload = json.loads(raw)
        # 人工审批即临时授权：把该工具加入 approved 让护栏放行，
        # 并以 certified 等级重放，因为审批人本身已是 staff。
        ctx.approved.add(payload["tool"])
        result = await self.registry.execute(payload["tool"], payload["args"], ctx, trust="certified")
        # 执行后立刻删除待审批 key，保证同一审批请求只能重放一次。
        await self.memory.redis.delete(f"shop:harness:pending:{run_id}")
        ledger = TraceLedger(run_id)
        ledger.append("approval.granted", tool=payload["tool"], approver=ctx.user_id)
        ledger.append("tool.result", tool=payload["tool"], status=result.status)
        return AgentRunResult(
            run_id=run_id,
            skill=payload.get("skill", ""),
            answer=f"审批已通过并执行 {payload['tool']}：{result.message or '完成'}",
            status="completed" if result.ok else "degraded",
            evidence=ledger.evidence_card(
                skill=payload.get("skill", ""),
                skill_hash="",
                trust="certified",
                model=self.provider.name,
                usage={},
                final_status="completed" if result.ok else "degraded",
            ),
            trace=ledger.events,
        )
