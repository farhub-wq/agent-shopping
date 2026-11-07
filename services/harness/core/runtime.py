"""Agent 运行时：ReAct 循环 + 权限护栏 + 记忆注入 + 证据产出。

设计取舍：不做「无限制自治」的通用 Agent，而是把每次运行约束在
「一个技能、一组工具、一个步数上限、一个可审批边界」内。
"""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shop_common.config import get_settings
from shop_common.errors import ApiError
from shop_common.logging import get_logger
from shop_common.metrics import AGENT_BUDGET_EXCEEDED

from .llm import MAX_PLAN_STEPS, Decision, LLMProvider
from .manifest import SkillManifest
from .memory import MemoryManager
from .tools import ToolContext, ToolRegistry
from .trace import TraceLedger

logger = get_logger(__name__)

settings = get_settings()

# 成本护栏话术：明确告知原因与出路，不伪装成模型产出
QUOTA_EXCEEDED_MESSAGE = "今日使用额度已用完，请明天再来或联系人工客服。"
RUN_BUDGET_EXCEEDED_MESSAGE = "本次对话超出单次运行的成本预算，请缩小问题范围或开启新会话。"

# 配额「查余量 + 扣预留」原子脚本：与库存扣减同理，check-and-set 必须落在
# Redis 单脚本内，否则并发 run 会在 check 与 charge 之间互相超支。
# 返回 -1 表示余量不足；否则返回预留后的账本值并刷新 TTL。
_QUOTA_ACQUIRE_LUA = """
local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
if spent + tonumber(ARGV[2]) > tonumber(ARGV[1]) then
  return -1
end
local new_spent = redis.call('INCRBY', KEYS[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], 172800)
return new_spent
"""


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

    async def _run_events(
        self,
        skill_name: str,
        user_message: str,
        ctx: ToolContext,
        *,
        max_steps: int | None = None,
        stream: bool,
    ) -> AsyncIterator[tuple[str, Any]]:
        """共享 ReAct 循环：run() 排空取结果，run_stream() 映射成 SSE 事件。

        产出三类元组：("trace", event) 关键工具事件（仅 stream 时）、
        ("delta", text) 答案分片（仅 stream 时）、("done", AgentRunResult) 收尾。
        """
        ledger = TraceLedger(ctx.run_id)
        manifest = self.skills.get(skill_name)
        if manifest is None:
            raise ApiError("NOT_FOUND", f"技能 {skill_name} 不存在")
        # 用户级每日配额：原子「查余量 + 扣预留」置于任何模型/嵌入调用之前，
        # 拿不到预留的运行零花费，优雅收尾（话术 + trace + 证据卡）而非 SSE 中段抛异常。
        quota_key = f"shop:harness:quota:{ctx.user_id}:{datetime.now(UTC):%Y%m%d}"
        reserve = settings.agent_run_token_budget
        acquired = await self.memory.redis.eval(
            _QUOTA_ACQUIRE_LUA, 1, quota_key, settings.agent_user_daily_token_quota, reserve
        )
        if acquired == -1:
            AGENT_BUDGET_EXCEEDED.labels("harness", "user").inc()
            await ledger.append("run.quota_exceeded", skill=skill_name, user=ctx.user_id)
            await ledger.append("run.end", status="quota_exceeded", answer=QUOTA_EXCEEDED_MESSAGE[:200])
            yield (
                "done",
                AgentRunResult(
                    run_id=ledger.run_id,
                    skill=skill_name,
                    answer=QUOTA_EXCEEDED_MESSAGE,
                    status="quota_exceeded",
                    evidence=ledger.evidence_card(
                        skill=skill_name,
                        skill_hash=manifest.content_hash,
                        trust=manifest.trust,
                        model="none",  # 未发生任何模型调用
                        usage={"estimated_tokens": 0},
                        final_status="quota_exceeded",
                    ),
                    trace=ledger.events,
                ),
            )
            return
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
        # 记录最终答案由哪个模型产出（降级/熔断后可能是备用模型），供证据卡审计。
        model_used = self.provider.name

        await ledger.append("run.start", skill=skill_name, trust=manifest.trust, user=ctx.user_id, message=user_message)

        for step in range(steps_limit):
            usage["steps"] = step + 1
            # 流式读模型决策：delta 帧是答案分片（仅 stream 时外抛），
            # terminal 帧才是本步实际决策；契约是 0..n 个 delta + 恰好一帧 terminal。
            decision: Decision | None = None
            async for frame in self.provider.decide_stream(system, messages, specs):
                if frame.kind == "delta":
                    if stream:
                        yield ("delta", frame.content)
                    continue
                decision = frame
                break
            assert decision is not None  # decide_stream 契约：必产一帧 terminal
            if decision.model:
                model_used = decision.model
            usage["prompt_chars"] += len(json.dumps(messages, ensure_ascii=False))
            step_event = await ledger.append(
                "agent.step",
                step=step + 1,
                kind=decision.kind,
                tool=decision.tool or None,
                content=(decision.content or "")[:200],
            )
            if stream:
                yield ("trace", step_event)
            if decision.kind == "final":
                answer = decision.content
                break

            # 单次运行预算：本步模型调用已发生，下一步会超支就停在这里；
            # 不执行本步工具——执行了也没预算让模型消化结果，反而留下半截动作。
            if usage["prompt_chars"] // 3 > settings.agent_run_token_budget:
                status = "budget_exceeded"
                answer = RUN_BUDGET_EXCEEDED_MESSAGE
                AGENT_BUDGET_EXCEEDED.labels("harness", "run").inc()
                budget_event = await ledger.append("run.budget_exceeded", prompt_chars=usage["prompt_chars"])
                if stream:
                    yield ("trace", budget_event)
                break

            # 把「单工具」与「计划」归一成有序步骤列表：plan 超出上限截断并记 trace，
            # 避免一次决策无限执行工具（NFR-10 工具调用有上限）。
            if decision.kind == "plan":
                steps = decision.steps[:MAX_PLAN_STEPS]
                if len(decision.steps) > MAX_PLAN_STEPS:
                    truncate_event = await ledger.append(
                        "plan.truncated", planned=len(decision.steps), executed=MAX_PLAN_STEPS
                    )
                    if stream:
                        yield ("trace", truncate_event)
            else:
                steps = [{"tool": decision.tool, "args": decision.args}]

            # 先记一条 assistant 消息（含全部 tool_calls），再逐 step 执行并回填 tool 结果，
            # 让消息历史对模型可见每一步的因果链（多 tool_calls 同属一轮 assistant 回合）。
            call_ids = [f"call_{uuid.uuid4().hex[:12]}" for _ in steps]
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_ids[index],
                            "type": "function",
                            "function": {
                                "name": step["tool"],
                                "arguments": json.dumps(step["args"], ensure_ascii=False),
                            },
                        }
                        for index, step in enumerate(steps)
                    ],
                }
            )

            halted = False
            for index, step in enumerate(steps):
                usage["tool_calls"] += 1
                call_event = await ledger.append("tool.call", tool=step["tool"], args=step["args"])
                if stream:
                    yield ("trace", call_event)
                result = await self.registry.execute(step["tool"], step["args"], ctx, trust=manifest.trust)
                result_event = await ledger.append(
                    "tool.result",
                    tool=step["tool"],
                    status=result.status,
                    duration_ms=result.duration_ms,
                    preview=str(result.data)[:200],
                )
                if stream:
                    yield ("trace", result_event)
                if result.status == "needs_approval":
                    # 不可逆动作到此为止：本步与后续 step 都不执行，转存待审批负载等人工处理。
                    status = "pending_approval"
                    pending = {"tool": step["tool"], "args": step["args"], "reason": result.message}
                    approval_event = await ledger.append("approval.requested", tool=step["tool"], args=step["args"])
                    if stream:
                        yield ("trace", approval_event)
                    halted = True
                    break
                if result.status in {"error", "denied"}:
                    # 工具失败不中断循环（模型还可换路），但整体标记 degraded，
                    # 保证最终答案不会被当成完整成功。
                    tool_errors += 1
                    status = "degraded" if status == "completed" else status
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_ids[index],
                        "content": json.dumps(result.to_dict(), ensure_ascii=False),
                    }
                )
            if halted:
                break
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
        # 结算：按「实际 - 预留」补扣/回补，单条 INCRBY 原子完成（负值即回补）。
        # 进程崩溃时预留量泄漏，随配额键 TTL 过期自愈——有界泄漏，护栏口径可接受。
        delta = usage["estimated_tokens"] - reserve
        usage["user_spent_today"] = await self.memory.redis.incrby(quota_key, delta) if delta else acquired

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
        await ledger.append("memory.write", user=ctx.user_id, skill=skill_name)
        await ledger.append("run.end", status=status, answer=answer[:200])

        evidence = ledger.evidence_card(
            skill=skill_name,
            skill_hash=manifest.content_hash,
            trust=manifest.trust,
            model=model_used,
            usage=usage,
            final_status=status,
        )
        yield (
            "done",
            AgentRunResult(
                run_id=ledger.run_id,
                skill=skill_name,
                answer=answer,
                status=status,
                evidence=evidence,
                trace=ledger.events,
                pending=pending,
            ),
        )

    async def run(
        self,
        skill_name: str,
        user_message: str,
        ctx: ToolContext,
        *,
        max_steps: int | None = None,
    ) -> AgentRunResult:
        # 非流式路径：排空共享生成器，忽略 delta/trace，只取收尾结果。
        async for kind, payload in self._run_events(
            skill_name, user_message, ctx, max_steps=max_steps, stream=False
        ):
            if kind == "done":
                return payload
        raise AssertionError("unreachable: _run_events 总以 done 收尾")

    async def run_stream(
        self, skill_name: str, user_message: str, ctx: ToolContext
    ) -> AsyncIterator[dict[str, Any]]:
        """SSE 友好：start 后工具轨迹实时推送、答案逐 delta 流出，final 收尾。"""
        manifest = self.skills.get(skill_name)
        if manifest is None:
            yield {"type": "error", "message": f"技能 {skill_name} 不存在"}
            return
        yield {"type": "start", "skill": skill_name, "trust": manifest.trust}
        async for kind, payload in self._run_events(skill_name, user_message, ctx, stream=True):
            if kind == "trace":
                yield {"type": "trace", "event": payload}
            elif kind == "delta":
                yield {"type": "delta", "text": payload}
            elif kind == "done":
                yield {"type": "final", "result": payload.to_dict()}

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
        await ledger.append("approval.granted", tool=payload["tool"], approver=ctx.user_id)
        await ledger.append("tool.result", tool=payload["tool"], status=result.status)
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
