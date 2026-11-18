"""运行时会话服务 — 消费定位策略并执行自动化操作。

这是新建的跨模块消费服务（不存在于 spec 的独立模块），
是元素定位链路的末端消费者。

数据流:
    channel("rpa.locator_strategies") ← element_selector
    → runtime_session 消费 → 模拟页面自动化执行
    → channel("rpa.automation_executed") → execution_log / locator_ledger

    locator_ledger (定位策略执行记录数据集，处理项癸)
    → runtime_session 按版本数据集消费、按最终选择器批量执行
    → 每个元素生成执行回执 → channel("rpa.locator_receipts")
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from typing import Any

from app.store import store

# 最终采用策略口径（与 locator_ledger 保持一致）
STRATEGY_PRIMARY = "主选择器"
STRATEGY_FALLBACK = "回退选择器"
STRATEGY_NONE = "无可用定位"


class RuntimeSessionService:
    """运行时会话管理 — 不继承 BaseModuleService，是纯跨模块消费者。"""

    SERVICE_NAME = "runtime_session"

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 结构化定位执行：主选择器 → 回退链，全程留痕
    # ------------------------------------------------------------------

    def _attempt_once(
        self,
        selector_type: str,
        expression: str,
        wait: str,
        rng: random.Random,
    ) -> tuple[bool, int, str]:
        """模拟单次选择器定位，返回 (是否命中, 耗时ms, 错误信息)。"""
        success_rates = {"id": 0.95, "css": 0.85, "xpath": 0.80, "text": 0.75}
        base_rate = success_rates.get(selector_type, 0.80)
        if wait == "visible":
            base_rate += 0.05
        elif wait == "none":
            base_rate -= 0.10
        success = rng.random() < base_rate
        duration_ms = rng.randint(50, 500)
        error = "" if success else self._generate_error(selector_type, expression, rng)
        return success, duration_ms, error

    def run_locator_attempts(
        self,
        primary: dict[str, str],
        fallback_chain: list[dict[str, str]],
        wait: str = "visible",
        rng: random.Random | None = None,
    ) -> dict[str, Any]:
        """执行一次完整的元素解析：主选择器优先，失败沿回退链恢复。

        每次解析只允许一个最终定位：
        - 主选择器命中 → 最终定位=主选择器，回退链另附为候选（不执行）；
        - 主选择器失败 → 依次尝试回退链，首个命中的成为最终定位；
        - 全部失败 → 最终策略=无可用定位，首次错误与回退路径完整保留。

        Args:
            primary: {"selector_type", "expression", "source"?}
            fallback_chain: [{"selector_type", "expression"}, ...]
            wait: 等待条件
            rng: 可注入的随机源（测试用），默认全局 random

        Returns:
            结构化执行结果（含 attempts/first_error/final_selector/
            final_strategy/fallback_candidates）
        """
        rng = rng or random
        attempts: list[dict[str, Any]] = []
        first_error = ""

        # 1) 主选择器（或台账记录指定的最终选择器）
        primary_source = primary.get("source") or STRATEGY_PRIMARY
        ok, ms, err = self._attempt_once(
            primary.get("selector_type", "css"),
            primary.get("expression", ""),
            wait, rng)
        attempts.append({
            "source": primary_source,
            "selector_type": primary.get("selector_type", "css"),
            "expression": primary.get("expression", ""),
            "success": ok,
            "error": err,
            "duration_ms": ms,
        })
        primary_success = ok
        if not ok:
            first_error = err  # 首次错误：主选择器失败即落账

        final_selector: dict[str, Any] | None = None
        final_strategy = STRATEGY_NONE
        fallback_candidates: list[dict[str, str]] = []

        if primary_success:
            # 主选择器命中：最终定位唯一；回退链只作为候选另附，不再执行
            final_selector = {
                "type": primary.get("selector_type", "css"),
                "expression": primary.get("expression", ""),
                "source": primary_source,
            }
            final_strategy = primary_source
            fallback_candidates = [dict(fb) for fb in fallback_chain]
        else:
            # 2) 主选择器失败：沿回退链恢复，首个命中者成为唯一最终定位
            for idx, fb in enumerate(fallback_chain, start=1):
                ok, ms, err = self._attempt_once(
                    fb.get("selector_type", "xpath"),
                    fb.get("expression", ""),
                    wait, rng)
                attempts.append({
                    "source": f"{STRATEGY_FALLBACK}{idx}",
                    "selector_type": fb.get("selector_type", "xpath"),
                    "expression": fb.get("expression", ""),
                    "success": ok,
                    "error": err,
                    "duration_ms": ms,
                })
                if not first_error and err:
                    first_error = err
                if ok:
                    final_selector = {
                        "type": fb.get("selector_type", "xpath"),
                        "expression": fb.get("expression", ""),
                        "source": STRATEGY_FALLBACK,
                    }
                    final_strategy = STRATEGY_FALLBACK
                    break

        success = final_selector is not None
        return {
            "attempts": attempts,
            "fallback_path": attempts,  # 回退路径全程留痕，供回执/台账核对
            "primary_success": primary_success,
            "first_error": first_error,
            "final_selector": final_selector,
            "final_strategy": final_strategy if success else STRATEGY_NONE,
            "fallback_candidates": fallback_candidates,
            "success": success,
            "duration_ms": sum(a["duration_ms"] for a in attempts),
        }

    # ------------------------------------------------------------------
    # 链路 A：消费 element_selector 发布的策略（既有口径，结构增强）
    # ------------------------------------------------------------------

    def consume_strategies_and_execute(self) -> dict[str, Any]:
        """消费定位策略并执行模拟自动化操作。

        从 channel("rpa.locator_strategies") 获取策略，
        模拟在浏览器会话中应用定位规则执行自动化。

        Returns:
            {"executed": int, "session_id": str, "results": [...]}
        """
        msgs = store.channel.consume(
            consumer=self.SERVICE_NAME,
            topic="rpa.locator_strategies",
        )
        if not msgs:
            return {"executed": 0, "session_id": None, "results": []}

        session_id = str(uuid.uuid4())[:8]
        session = {
            "session_id": session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "strategies_applied": 0,
            "actions_executed": 0,
            "errors": 0,
        }
        results = []

        for msg in msgs:
            strategy = msg.payload
            action_result = self._simulate_automation_action(strategy)

            if action_result["success"]:
                session["actions_executed"] += 1
            else:
                session["errors"] += 1
                # 记录失败到恢复管理器：首次错误 + 最终采用策略
                store.recovery.log(
                    module=self.SERVICE_NAME,
                    batch_ref=session_id,
                    step=(
                        f"apply_locator:{strategy.get('rule_name', 'unknown')}|"
                        f"最终策略:{action_result.get('final_strategy', STRATEGY_NONE)}"
                    ),
                    error_detail=action_result.get("first_error")
                    or action_result.get("error", "unknown"),
                )

            session["strategies_applied"] += 1
            results.append(action_result)
            store.channel.confirm(msg.msg_id, self.SERVICE_NAME)

        # 发布执行结果
        store.channel.publish(
            topic="rpa.automation_executed",
            source=self.SERVICE_NAME,
            payload={
                "session_id": session_id,
                "total_strategies": len(msgs),
                "successful": session["actions_executed"],
                "failed": session["errors"],
                "results": results,
            },
        )

        self._sessions[session_id] = session
        return {
            "executed": session["actions_executed"],
            "session_id": session_id,
            "total": len(msgs),
            "errors": session["errors"],
            "results": results,
        }

    def _simulate_automation_action(self, strategy: dict[str, Any]) -> dict[str, Any]:
        """模拟自动化操作执行（模拟浏览器元素交互）。

        结构化结果：主/回退每次尝试都留痕，首次错误与最终采用策略
        始终可核对；同时保留 success/action/duration_ms 等既有字段。
        """
        from app.services.locator_ledger import parse_fallback_chain

        fallback_chain = parse_fallback_chain(strategy.get("fallback", ""))
        exec_result = self.run_locator_attempts(
            primary={
                "selector_type": strategy.get("selector_type", "css"),
                "expression": strategy.get("expression", ""),
            },
            fallback_chain=fallback_chain,
            wait=strategy.get("wait_condition", "visible"),
        )

        result: dict[str, Any] = {
            "rule_id": strategy.get("rule_id"),
            "rule_name": strategy.get("rule_name"),
            "selector_type": strategy.get("selector_type", "css"),
            "expression": str(strategy.get("expression", ""))[:50],
            **exec_result,
        }
        if exec_result["success"]:
            final = exec_result["final_selector"] or {}
            result["action"] = (
                f"定位成功: {strategy.get('rule_name', 'N/A')}"
                f"（{exec_result['final_strategy']}）"
            )
            result["element_found"] = True
            result["final_expression"] = final.get("expression", "")
        else:
            result["action"] = "定位失败"
            result["error"] = exec_result["first_error"] or "元素定位失败"
        return result

    # ------------------------------------------------------------------
    # 链路 B：消费定位策略执行记录数据集（处理项癸），批量执行并生成回执
    # ------------------------------------------------------------------

    def execute_record(
        self,
        record: dict[str, Any],
        session_id: str | None = None,
        rng: random.Random | None = None,
    ) -> tuple[dict[str, Any], str]:
        """按台账记录的最终选择器执行一次元素解析。

        以记录的最终选择器为唯一入口；失败时沿回退链恢复并更新最终定位。
        供批量执行与异常重测共用。

        Returns:
            (结构化执行结果, session_id)
        """
        session_id = session_id or str(uuid.uuid4())[:8]
        final = record.get("final_selector") or {}
        exec_result = self.run_locator_attempts(
            primary={
                "selector_type": final.get("type", "css"),
                "expression": final.get("expression", ""),
                "source": final.get("source") or STRATEGY_PRIMARY,
            },
            fallback_chain=record.get("fallback_chain", []),
            wait="visible",
            rng=rng,
        )
        exec_result["rule_id"] = record.get("rule_id")
        exec_result["rule_name"] = record.get("rule_name")
        exec_result["element"] = record.get("element")
        return exec_result, session_id

    def consume_ledger_and_execute(
        self,
        version: str | None = None,
    ) -> dict[str, Any]:
        """消费定位策略执行记录数据集，按最终选择器批量执行并生成回执。

        读取处理项癸（locator_ledger）指定版本数据集中有待办的记录，
        逐元素按最终选择器执行，每个元素生成一条执行回执并回写台账，
        联动更新待办状态。

        Returns:
            {"version", "session_id", "executed", "failed", "receipts": [...]}
        """
        from app.services.locator_ledger import locator_ledger

        version = version or locator_ledger.latest_version()
        if version is None:
            return {"executed": 0, "session_id": None, "receipts": [],
                    "message": "台账数据集为空，请先构建定位策略执行记录数据集"}

        records = locator_ledger.pending_records(version)
        if not records:
            return {"executed": 0, "session_id": None, "version": version,
                    "receipts": [], "message": f"版本 {version} 没有待执行记录"}

        session_id = str(uuid.uuid4())[:8]
        session = {
            "session_id": session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "dataset_version": version,
            "strategies_applied": 0,
            "actions_executed": 0,
            "errors": 0,
            "receipts_generated": 0,
        }
        receipts: list[dict[str, Any]] = []

        for record in records:
            exec_result, _ = self.execute_record(record, session_id=session_id)
            receipt = locator_ledger.register_receipt(
                record["record_id"], exec_result, session_id)
            session["strategies_applied"] += 1
            if exec_result["success"]:
                session["actions_executed"] += 1
            else:
                session["errors"] += 1
            if receipt:
                session["receipts_generated"] += 1
                receipts.append(receipt)

        # 发布回执批次事件
        store.channel.publish(
            topic="rpa.locator_receipts",
            source=self.SERVICE_NAME,
            payload={
                "session_id": session_id,
                "dataset_version": version,
                "total": len(records),
                "successful": session["actions_executed"],
                "failed": session["errors"],
                "receipt_ids": [r["receipt_id"] for r in receipts],
            },
        )

        self._sessions[session_id] = session
        return {
            "version": version,
            "session_id": session_id,
            "executed": session["actions_executed"],
            "failed": session["errors"],
            "total": len(records),
            "receipts": receipts,
        }

    # ------------------------------------------------------------------
    # 查询与快照
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_error(
        selector_type: str,
        expression: str,
        rng: random.Random | None = None,
    ) -> str:
        rng = rng or random
        errors = [
            f"选择器超时: 元素 {expression[:30]} 在等待时间内未出现",
            "元素被遮挡，无法交互",
            f"页面动态加载导致 {selector_type} 选择器失效",
            "元素在 iframe 内，需切换上下文",
        ]
        return rng.choice(errors)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有运行中的会话。"""
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def domain_snapshot(self) -> dict[str, Any]:
        return {
            "module": self.SERVICE_NAME,
            "entity": "运行时会话",
            "artifact": "自动化执行记录",
            "active_sessions": len(self._sessions),
            "actions_executed": sum(
                s.get("actions_executed", 0) for s in self._sessions.values()),
            "receipts_generated": sum(
                s.get("receipts_generated", 0) for s in self._sessions.values()),
            "automation_events": len(
                store.channel.snapshot(topic="rpa.automation_executed")),
            "locator_receipt_events": len(
                store.channel.snapshot(topic="rpa.locator_receipts")),
        }


# 全局运行时实例
runtime_session = RuntimeSessionService()
