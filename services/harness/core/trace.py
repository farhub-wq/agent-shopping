"""证据账本：每次 Agent 运行都产出可核对的 trace 与证据卡。

证据卡不是日志摘要，而是「这次运行做过了什么、花了多少、有没有越权」的可验证声明。
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACE_ROOT = Path(".harness/traces")


class TraceLedger:
    def __init__(self, run_id: str | None = None, root: Path = TRACE_ROOT) -> None:
        self.run_id = run_id or uuid.uuid4().hex
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{self.run_id}.jsonl"
        self.events: list[dict[str, Any]] = []
        self._seq = 0

    def append(self, event_type: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        event = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **fields,
        }
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event

    def digest(self) -> str:
        body = "\n".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in self.events
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def evidence_card(
        self,
        *,
        skill: str,
        skill_hash: str,
        trust: str,
        model: str,
        usage: dict[str, Any],
        final_status: str,
    ) -> dict[str, Any]:
        tool_events = [item for item in self.events if item["type"] == "tool.result"]
        denied = [item for item in tool_events if item.get("status") in {"denied", "needs_approval"}]
        return {
            "run_id": self.run_id,
            "skill": skill,
            "skill_hash": skill_hash,
            "trust": trust,
            "model": model,
            "final_status": final_status,
            "steps": sum(1 for item in self.events if item["type"] == "agent.step"),
            "tool_calls": len(tool_events),
            "denied_calls": len(denied),
            "usage": usage,
            "trace_digest": self.digest(),
            "trace_path": str(self.path),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


def load_trace(run_id: str, root: Path = TRACE_ROOT) -> list[dict[str, Any]]:
    path = root / f"{run_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
