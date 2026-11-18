"""定位策略执行台账 — 记录每次元素解析的最终选择器与回退证据。

这是新建的跨模块台账服务（处理项癸），解决"定位策略发布后没有批量执行
与回退证据"的问题：执行结果不再只保留成功计数，每条失败记录都能核对
最终采用的选择器和回退路径。

数据流:
    element_selector (已启用定位规则) + runtime_session (新到达的执行会话记录)
    → locator_ledger.build_dataset 解析主/回退执行结果
    → 定位策略执行记录数据集（版本化，重复导入只认第一条）
    → runtime_session 按版本数据集消费、按最终选择器批量执行
    → 每个元素生成执行回执（channel "rpa.locator_receipts"）

核心口径:
    - 每次元素解析只允许一个最终定位（final_selector 唯一）供下游使用；
    - 主选择器与回退选择器都能命中时，保留主结果为最终定位，
      回退链另附为候选（fallback_candidates），不产生第二个可执行定位；
    - 失败恢复记录必须保存首次错误（first_error）和最终采用策略
      （final_strategy）；
    - 重复导入只认第一条记录（按 rule_id 去重）。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

# 台账记录状态口径
RECORD_STATUSES = ["待执行", "执行成功", "执行失败", "异常"]

# 最终采用策略口径
STRATEGY_PRIMARY = "主选择器"
STRATEGY_FALLBACK = "回退选择器"
STRATEGY_NONE = "无可用定位"


def parse_fallback_chain(raw: str) -> list[dict[str, str]]:
    """把定位规则的「回退方案」文本解析为结构化回退链。

    支持 "type:expression" 前缀，多个回退用 ";" 或 "|" 分隔；
    没有类型前缀时默认按 xpath 处理（兼容既有业务口径）。

    >>> parse_fallback_chain("css:.next;xpath://a[@rel='next']")
    [{'selector_type': 'css', 'expression': '.next'},
     {'selector_type': 'xpath', 'expression': "//a[@rel='next']"}]
    """
    chain: list[dict[str, str]] = []
    for part in str(raw or "").replace("|", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part and part.split(":", 1)[0].strip() in (
            "css", "xpath", "id", "text", "attribute",
        ):
            stype, expr = part.split(":", 1)
            chain.append({"selector_type": stype.strip(), "expression": expr.strip()})
        else:
            chain.append({"selector_type": "xpath", "expression": part})
    return chain


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_fallback_path(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把执行尝试轨迹归一化为台账回退路径结构。

    无论来自会话记录解析还是回执回写，落账结构保持一致，
    避免同一字段两种结构造成的显示错位。
    """
    return [
        {
            "source": a.get("source"),
            "selector_type": a.get("selector_type"),
            "expression": a.get("expression"),
            "outcome": "命中" if a.get("success") else "失败",
            "error": a.get("error", ""),
        }
        for a in attempts or []
    ]


class LocatorLedgerService:
    """定位策略执行台账 — 版本化数据集 + 回执登记 + 核对。

    不继承 BaseModuleService：台账记录由执行链路生成，不走通用登记入口，
    避免绕过解析直接写入脏记录。
    """

    SERVICE_NAME = "locator_ledger"
    ENTITY = "定位策略执行记录"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._receipt_seq = 0
        self._version_seq = 0
        self._records: list[dict[str, Any]] = []
        self._receipts: list[dict[str, Any]] = []
        self._datasets: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 数据集构建：读取已启用规则 + 新到达的运行时会话记录
    # ------------------------------------------------------------------

    def build_dataset(self, rule_ids: list[int] | None = None) -> dict[str, Any]:
        """构建定位策略执行记录数据集（新版本）。

        数据来源:
            1. element_selector 中状态为「已启用」的定位规则；
            2. channel("rpa.automation_executed") 中新到达的运行时会话记录
               （未被台账消费过的），解析其中的主/回退执行结果。

        重复导入只认第一条记录：同一 rule_id 已存在台账记录时，
        后续到达的会话结果不再覆盖，计入 skipped_duplicates。

        Returns:
            本次构建的版本信息：{"version", "created", "skipped_duplicates", ...}
        """
        # 1) 消费新到达的运行时会话记录，按 rule_id 索引最新执行结果
        session_results: dict[int, dict[str, Any]] = {}
        msgs = store.channel.consume(
            consumer=self.SERVICE_NAME, topic="rpa.automation_executed")
        for msg in msgs:
            payload = msg.payload
            for res in payload.get("results", []):
                rid = res.get("rule_id")
                if rid is None:
                    continue
                session_results[int(rid)] = {
                    **res,
                    "source_session": payload.get("session_id"),
                }
            store.channel.confirm(msg.msg_id, self.SERVICE_NAME)

        # 2) 读取已启用定位规则
        rules = [
            dict(r) for r in store.rows("element_selector")
            if r.get("status") == "已启用"
        ]
        if rule_ids:
            wanted = {int(rid) for rid in rule_ids}
            rules = [r for r in rules if int(r.get("id", 0)) in wanted]

        with self._lock:
            self._version_seq += 1
            version = f"v{self._version_seq}"
            created = 0
            skipped = 0
            known_rules = {rec["rule_id"] for rec in self._records}

            for rule in rules:
                rid = int(rule.get("id", 0))
                if rid in known_rules:
                    # 重复导入只认第一条记录
                    skipped += 1
                    continue
                known_rules.add(rid)
                session_hit = session_results.get(rid)
                record = self._record_from_rule(rule, version, session_hit)
                self._records.append(record)
                created += 1

            dataset = {
                "version": version,
                "created_at": _utcnow(),
                "created": created,
                "skipped_duplicates": skipped,
                "session_msgs_consumed": len(msgs),
                "rule_scope": sorted({r.get("id") for r in rules}),
            }
            self._datasets.append(dataset)

        # 3) 发布数据集就绪事件，供运行时会话消费
        store.channel.publish(
            topic="rpa.locator_ledger_ready",
            source=self.SERVICE_NAME,
            payload={
                "version": version,
                "created": created,
                "skipped_duplicates": skipped,
            },
        )
        return dataset

    def _record_from_rule(
        self,
        rule: dict[str, Any],
        version: str,
        session_hit: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """把定位规则（+可选的会话执行结果）解析为一条台账记录。

        每条记录只保留一个最终定位；失败记录必须带首次错误与最终采用策略。
        """
        self._seq += 1
        fallback_chain = parse_fallback_chain(rule.get("回退方案", ""))
        record: dict[str, Any] = {
            "record_id": f"LER-{self._seq:04d}",
            "dataset_version": version,
            "rule_id": int(rule.get("id", 0)),
            "rule_name": rule.get("规则名称", ""),
            "element": rule.get("规则名称", ""),
            "bot_ref": rule.get("所属流程", ""),
            "primary_selector": {
                "type": rule.get("选择器类型", "css"),
                "expression": rule.get("定位表达式", ""),
            },
            "fallback_chain": fallback_chain,
            "primary_outcome": "未执行",
            "first_error": "",
            "fallback_path": [],
            "fallback_candidates": [],
            "final_selector": {
                "type": rule.get("选择器类型", "css"),
                "expression": rule.get("定位表达式", ""),
                "source": STRATEGY_PRIMARY,
            },
            "final_strategy": STRATEGY_PRIMARY,
            "session_id": None,
            "source_session": None,
            "receipt_id": None,
            "status": "待执行",
            "pending": True,
            "abnormal": bool(rule.get("abnormal")),
            "updated_at": _utcnow(),
        }
        if session_hit:
            self._apply_session_result(record, session_hit)
        return record

    def _apply_session_result(
        self, record: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """把运行时会话的执行结果解析进台账记录（主/回退证据落账）。"""
        attempts = result.get("attempts") or []
        record["session_id"] = result.get("source_session")
        record["source_session"] = result.get("source_session")
        record["primary_outcome"] = "命中" if result.get("primary_success") else "失败"
        record["first_error"] = result.get("first_error", "") or ""
        record["fallback_path"] = normalize_fallback_path(attempts)
        record["fallback_candidates"] = result.get("fallback_candidates", []) or []
        final = result.get("final_selector")
        if final:
            record["final_selector"] = final
        record["final_strategy"] = result.get("final_strategy", STRATEGY_PRIMARY)
        if result.get("success"):
            record["status"] = "执行成功"
            record["abnormal"] = False
        else:
            record["status"] = "执行失败"
            record["abnormal"] = True
        record["updated_at"] = _utcnow()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._datasets)

    def latest_version(self) -> str | None:
        with self._lock:
            return self._datasets[-1]["version"] if self._datasets else None

    def list_records(
        self,
        version: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._records)
        if version:
            rows = [r for r in rows if r["dataset_version"] == version]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return rows

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            for rec in self._records:
                if rec["record_id"] == record_id:
                    return rec
        return None

    def pending_records(self, version: str | None = None) -> list[dict[str, Any]]:
        """有待办的记录：待执行 / 执行失败 / 异常，供批量执行消费。"""
        return [
            r for r in self.list_records(version=version)
            if r.get("pending") or r.get("status") in ("待执行", "执行失败", "异常")
        ]

    # ------------------------------------------------------------------
    # 回执登记（由运行时会话批量执行/重测后回写）
    # ------------------------------------------------------------------

    def register_receipt(
        self,
        record_id: str,
        exec_result: dict[str, Any],
        session_id: str,
        supersede_old: bool = True,
    ) -> dict[str, Any] | None:
        """为一条台账记录登记执行回执，并回写最终定位结论。

        每次元素解析只允许一个最终结果：回执回写后记录里只保留
        一个 final_selector；重测产生新回执时旧回执标记 superseded，
        保证任一时刻每条记录只有一条有效回执（结果明细对得上业务记录）。
        """
        with self._lock:
            record = next(
                (r for r in self._records if r["record_id"] == record_id), None)
            if record is None:
                return None

            if supersede_old:
                for old in self._receipts:
                    if old["record_id"] == record_id and not old["superseded"]:
                        old["superseded"] = True

            self._receipt_seq += 1
            receipt = {
                "receipt_id": f"RCPT-{self._receipt_seq:04d}",
                "record_id": record_id,
                "dataset_version": record["dataset_version"],
                "rule_id": record["rule_id"],
                "element": record["element"],
                "final_selector": exec_result.get("final_selector"),
                "final_strategy": exec_result.get("final_strategy"),
                "outcome": "命中" if exec_result.get("success") else "失败",
                "first_error": exec_result.get("first_error", "") or "",
                "fallback_path": normalize_fallback_path(
                    exec_result.get("fallback_path")),
                "session_id": session_id,
                "duration_ms": exec_result.get("duration_ms", 0),
                "superseded": False,
                "created_at": _utcnow(),
            }
            self._receipts.append(receipt)

            # 回写记录：最终采用的选择器与回退路径（失败记录必须可核对）
            record["primary_outcome"] = (
                "命中" if exec_result.get("primary_success") else "失败")
            record["first_error"] = exec_result.get("first_error", "") or ""
            record["fallback_path"] = receipt["fallback_path"]
            record["fallback_candidates"] = exec_result.get("fallback_candidates", [])
            if exec_result.get("final_selector"):
                record["final_selector"] = exec_result["final_selector"]
            record["final_strategy"] = exec_result.get(
                "final_strategy", STRATEGY_NONE)
            record["session_id"] = session_id
            record["receipt_id"] = receipt["receipt_id"]
            record["status"] = "执行成功" if exec_result.get("success") else "执行失败"
            record["pending"] = False  # 联动更新待办
            record["abnormal"] = not exec_result.get("success")
            record["updated_at"] = _utcnow()

            # 联动更新定位规则待办
            rule = store.find("element_selector", record["rule_id"])
            if rule is not None:
                rule["pending"] = False
                rule["abnormal"] = record["abnormal"]

        # 失败恢复记录：保存首次错误和最终采用策略
        if record["first_error"]:
            store.recovery.log(
                module=self.SERVICE_NAME,
                batch_ref=session_id,
                step=(
                    f"locator:{record['rule_name']}|"
                    f"最终策略:{record['final_strategy']}"
                ),
                error_detail=record["first_error"],
            )
        return receipt

    def list_receipts(
        self,
        record_id: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._receipts)
        if record_id:
            rows = [r for r in rows if r["record_id"] == record_id]
        if active_only:
            rows = [r for r in rows if not r["superseded"]]
        return rows

    # ------------------------------------------------------------------
    # 异常元素重测：生成新的执行回执
    # ------------------------------------------------------------------

    def retest_records(self, record_ids: list[str]) -> dict[str, Any]:
        """对异常元素重新测试，每条生成新的执行回执（旧回执失效）。

        执行器由运行时会话提供，这里只负责台账登记；
        延迟导入避免与 runtime_session 循环依赖。
        """
        from app.services.runtime_session import runtime_session

        results: list[dict[str, Any]] = []
        retested = 0
        for record_id in record_ids:
            record = self.get_record(record_id)
            if record is None:
                results.append({"record_id": record_id, "ok": False,
                                "message": "台账记录不存在"})
                continue
            exec_result, session_id = runtime_session.execute_record(record)
            receipt = self.register_receipt(record_id, exec_result, session_id)
            retested += 1
            results.append({
                "record_id": record_id,
                "ok": True,
                "receipt_id": receipt["receipt_id"] if receipt else None,
                "final_strategy": record["final_strategy"],
                "status": record["status"],
                "session_id": session_id,
            })
        return {"retested": retested, "results": results}

    # ------------------------------------------------------------------
    # 核对：业务记录 vs 结果明细（残留 / 丢失 / 错位 / 重复）
    # ------------------------------------------------------------------

    def reconcile(self) -> dict[str, Any]:
        """核对台账记录与回执明细是否对得上。

        - 残留：记录已办结（待办清零）且状态为已执行类，但没有有效回执
          （仍在待办中的记录尚未到回执环节，不算残留）；
        - 丢失：有效回执指向的台账记录不存在；
        - 错位：有效回执的最终选择器与台账记录不一致；
        - 重复：同一记录存在多条有效回执。
        """
        with self._lock:
            records = list(self._records)
            active_receipts = [r for r in self._receipts if not r["superseded"]]

        record_by_id = {r["record_id"]: r for r in records}
        receipts_by_record: dict[str, list[dict[str, Any]]] = {}
        for rc in active_receipts:
            receipts_by_record.setdefault(rc["record_id"], []).append(rc)

        residual = [
            {"record_id": r["record_id"], "rule_name": r["rule_name"],
             "status": r["status"]}
            for r in records
            if not r.get("pending")
            and r["status"] in ("执行成功", "执行失败")
            and not receipts_by_record.get(r["record_id"])
        ]
        missing = [
            {"receipt_id": rc["receipt_id"], "record_id": rc["record_id"]}
            for rc in active_receipts
            if rc["record_id"] not in record_by_id
        ]
        misaligned = []
        for rc in active_receipts:
            rec = record_by_id.get(rc["record_id"])
            if rec is None:
                continue
            final = rec.get("final_selector") or {}
            rc_final = rc.get("final_selector") or {}
            if (final.get("expression") != rc_final.get("expression")
                    or rec.get("final_strategy") != rc.get("final_strategy")):
                misaligned.append({
                    "record_id": rec["record_id"],
                    "record_final": final.get("expression"),
                    "receipt_id": rc["receipt_id"],
                    "receipt_final": rc_final.get("expression"),
                })
        duplicate = [
            {"record_id": rid, "receipts": [rc["receipt_id"] for rc in rcs]}
            for rid, rcs in receipts_by_record.items()
            if len(rcs) > 1
        ]

        consistent = not (residual or missing or misaligned or duplicate)
        return {
            "记录总数": len(records),
            "有效回执数": len(active_receipts),
            "残留": residual,
            "丢失": missing,
            "错位": misaligned,
            "重复": duplicate,
            "consistent": consistent,
        }

    # ------------------------------------------------------------------
    # 快照（兼容既有业务口径：module/entity/artifact/count/by_status/abnormal）
    # ------------------------------------------------------------------

    def domain_snapshot(self) -> dict[str, Any]:
        records = self.list_records()
        by_status = {
            status: sum(1 for r in records if r.get("status") == status)
            for status in RECORD_STATUSES
        }
        receipts = self.list_receipts(active_only=True)
        return {
            "module": self.SERVICE_NAME,
            "entity": self.ENTITY,
            "artifact": "定位策略执行记录数据集",
            "count": len(records),
            "by_status": by_status,
            "abnormal": sum(1 for r in records if r.get("abnormal")),
            "pending": sum(1 for r in records if r.get("pending")),
            "dataset_versions": len(self.list_datasets()),
            "latest_version": self.latest_version(),
            "active_receipts": len(receipts),
            "fallback_recovered": sum(
                1 for r in records if r.get("final_strategy") == STRATEGY_FALLBACK),
            "unresolvable": sum(
                1 for r in records if r.get("final_strategy") == STRATEGY_NONE),
        }


# 全局台账实例
locator_ledger = LocatorLedgerService()
