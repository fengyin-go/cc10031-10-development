"""定位策略执行台账（处理项癸）测试。

覆盖验收口径：
1. 读取已启用定位规则 + 新到达运行时会话记录 → 生成执行记录数据集；
2. 每次元素解析只有一个最终定位；主/回退冲突时保留主结果并另附回退候选；
3. 失败记录必须保存首次错误和最终采用策略，回退路径可核对；
4. 运行时会话按版本数据集批量执行并为每个元素生成回执，联动更新待办；
5. 重复导入只认第一条记录；
6. 业务记录和结果明细对得上（残留/丢失/错位/重复核对）；
7. 异常元素重测生成新回执；
8. 兼容既有业务口径。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.element_selector import ElementSelectorService
from app.services.locator_ledger import (
    LocatorLedgerService,
    locator_ledger,
    parse_fallback_chain,
)
from app.services.runtime_session import RuntimeSessionService, runtime_session


# ── 测试工具 ──

class FixedRng:
    """按预定序列返回随机值的 rng，用于强制主/回退的命中与失败。"""

    def __init__(self, values: list[float]):
        self._values = list(values)
        self._i = 0

    def random(self) -> float:
        value = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return value

    def randint(self, a: int, b: int) -> int:
        return a

    def choice(self, seq):
        return seq[0]


_RULE_SEQ = {"next": 900}


def add_enabled_rule(name: str, fallback: str = "xpath://fallback") -> dict:
    """向数据仓库追加一条已启用定位规则，返回该行。"""
    _RULE_SEQ["next"] += 1
    rid = _RULE_SEQ["next"]
    row = {
        "id": rid,
        "status": "已启用",
        "pending": True,
        "abnormal": False,
        "规则名称": name,
        "选择器类型": "css",
        "定位表达式": f"#elem-{rid}",
        "匹配策略": "first_match",
        "等待条件": "visible",
        "回退方案": fallback,
        "所属流程": "台账测试流程",
    }
    store.rows("element_selector").append(row)
    return row


# ── 回退链解析 ──

class TestParseFallbackChain:
    def test_typed_and_multi_entries(self) -> None:
        chain = parse_fallback_chain("css:.next;xpath://a[@rel='next']")
        assert chain == [
            {"selector_type": "css", "expression": ".next"},
            {"selector_type": "xpath", "expression": "//a[@rel='next']"},
        ]

    def test_default_type_is_xpath(self) -> None:
        chain = parse_fallback_chain("//button[1]")
        assert chain == [{"selector_type": "xpath", "expression": "//button[1]"}]

    def test_empty_and_separator_tolerance(self) -> None:
        assert parse_fallback_chain("") == []
        assert parse_fallback_chain(None) == []
        assert len(parse_fallback_chain("css:.a|//b")) == 2


# ── 元素解析唯一最终定位 ──

class TestSingleFinalLocator:
    def test_primary_hit_conflict_keeps_primary_and_attaches_candidates(self) -> None:
        """主选择器与回退选择器都能命中时：保留主结果为最终定位，
        回退另附为候选，只允许一个可执行定位。"""
        svc = RuntimeSessionService()
        result = svc.run_locator_attempts(
            primary={"selector_type": "css", "expression": "#ok"},
            fallback_chain=[{"selector_type": "xpath", "expression": "//ok"}],
            rng=FixedRng([0.0]),  # 主选择器必中
        )
        assert result["success"] is True
        assert result["final_strategy"] == "主选择器"
        assert result["final_selector"]["expression"] == "#ok"
        # 回退链另附为候选，且未真正执行（只有主选择器一次尝试）
        assert result["fallback_candidates"] == [
            {"selector_type": "xpath", "expression": "//ok"}]
        assert len(result["attempts"]) == 1
        # 只有一个可执行定位
        assert result["final_selector"]["source"] == "主选择器"

    def test_primary_fail_fallback_becomes_final(self) -> None:
        """主选择器失败、回退命中：回退成为唯一最终定位，首次错误落账。"""
        svc = RuntimeSessionService()
        result = svc.run_locator_attempts(
            primary={"selector_type": "css", "expression": "#broken"},
            fallback_chain=[{"selector_type": "xpath", "expression": "//backup"}],
            rng=FixedRng([0.999, 0.0]),  # 主失败、回退中
        )
        assert result["success"] is True
        assert result["final_strategy"] == "回退选择器"
        assert result["final_selector"]["expression"] == "//backup"
        assert result["first_error"] != ""
        assert len(result["fallback_path"]) == 2
        assert result["fallback_path"][0]["source"] == "主选择器"
        assert result["fallback_path"][1]["source"].startswith("回退选择器")

    def test_all_fail_marks_unresolvable_but_keeps_evidence(self) -> None:
        """主/回退全部失败：最终策略=无可用定位，首次错误与回退路径保留。"""
        svc = RuntimeSessionService()
        result = svc.run_locator_attempts(
            primary={"selector_type": "css", "expression": "#gone"},
            fallback_chain=[{"selector_type": "xpath", "expression": "//gone"}],
            rng=FixedRng([0.999, 0.999]),
        )
        assert result["success"] is False
        assert result["final_strategy"] == "无可用定位"
        assert result["final_selector"] is None
        assert result["first_error"] != ""
        assert len(result["fallback_path"]) == 2


# ── 数据集构建 ──

class TestBuildDataset:
    def test_build_from_enabled_rules(self) -> None:
        rule = add_enabled_rule("台账构建验证元素")
        ledger = LocatorLedgerService()
        dataset = ledger.build_dataset(rule_ids=[rule["id"]])
        assert dataset["created"] == 1
        records = ledger.list_records(version=dataset["version"])
        assert len(records) == 1
        rec = records[0]
        # 每条记录只有一个最终定位
        assert rec["final_selector"]["expression"] == f"#elem-{rule['id']}"
        assert rec["final_strategy"] == "主选择器"
        assert rec["status"] == "待执行"
        assert rec["pending"] is True

    def test_build_parses_runtime_session_results(self) -> None:
        """新到达的运行时会话记录被解析：主/回退结果、首次错误落账。"""
        rule = add_enabled_rule("会话解析验证元素")
        rule["status"] = "已测试"  # publish 需要 草稿/已测试
        elem_svc = ElementSelectorService()
        published = elem_svc.publish_locator_strategy(rule["id"])
        assert published is not None
        exec_result = runtime_session.consume_strategies_and_execute()
        assert exec_result["total"] >= 1

        dataset = locator_ledger.build_dataset(rule_ids=[rule["id"]])
        assert dataset["session_msgs_consumed"] >= 1
        rec = locator_ledger.list_records(version=dataset["version"])[0]
        assert rec["primary_outcome"] in ("命中", "失败")
        assert rec["source_session"] == exec_result["session_id"]
        if rec["primary_outcome"] == "失败":
            # 失败记录必须保存首次错误和最终采用策略
            assert rec["first_error"] != ""
            assert rec["final_strategy"] in ("回退选择器", "无可用定位")
            assert len(rec["fallback_path"]) >= 1

    def test_duplicate_import_keeps_first_record(self) -> None:
        """重复导入只认第一条记录。"""
        rule = add_enabled_rule("重复导入验证元素")
        ledger = LocatorLedgerService()
        first = ledger.build_dataset(rule_ids=[rule["id"]])
        second = ledger.build_dataset(rule_ids=[rule["id"]])
        assert first["created"] == 1
        assert second["created"] == 0
        assert second["skipped_duplicates"] == 1
        records = [r for r in ledger.list_records() if r["rule_id"] == rule["id"]]
        assert len(records) == 1
        assert records[0]["dataset_version"] == first["version"]


# ── 批量执行与回执 ──

class TestBatchExecuteAndReceipts:
    def test_execute_generates_receipt_per_element(self) -> None:
        """运行时会话按版本数据集批量执行：每个元素一条回执，联动更新待办。"""
        rules = [add_enabled_rule(f"批量执行验证元素{i}") for i in range(3)]
        rule_ids = [r["id"] for r in rules]
        dataset = locator_ledger.build_dataset(rule_ids=rule_ids)
        version = dataset["version"]

        result = runtime_session.consume_ledger_and_execute(version=version)
        assert result["total"] == 3
        assert len(result["receipts"]) == 3  # 每个元素生成回执

        records = locator_ledger.list_records(version=version)
        receipt_ids = {r["receipt_id"] for r in result["receipts"]}
        for rec in records:
            # 业务记录和结果明细对得上
            assert rec["receipt_id"] in receipt_ids
            assert rec["pending"] is False  # 联动更新待办
            assert rec["session_id"] == result["session_id"]
            rule = store.find("element_selector", rec["rule_id"])
            assert rule["pending"] is False  # 定位规则待办联动

    def test_failed_records_show_final_selector_and_fallback_path(self) -> None:
        """每条失败记录都能看到最终采用的选择器和回退路径。"""
        rule = add_enabled_rule("失败证据验证元素")
        dataset = locator_ledger.build_dataset(rule_ids=[rule["id"]])
        runtime_session.consume_ledger_and_execute(version=dataset["version"])
        rec = locator_ledger.list_records(version=dataset["version"])[0]
        assert rec["final_strategy"] in ("主选择器", "回退选择器", "无可用定位")
        if rec["status"] == "执行失败":
            assert rec["first_error"] != ""
            assert len(rec["fallback_path"]) >= 1
            # 回退路径每一步都有来源与结果
            for step in rec["fallback_path"]:
                assert step["source"]
                assert step["outcome"] in ("命中", "失败")

    def test_reconcile_consistent_after_execute(self) -> None:
        """核对：执行后业务记录与结果明细对得上（无残留/丢失/错位/重复）。"""
        rules = [add_enabled_rule(f"核对验证元素{i}") for i in range(2)]
        dataset = locator_ledger.build_dataset(
            rule_ids=[r["id"] for r in rules])
        runtime_session.consume_ledger_and_execute(version=dataset["version"])
        report = locator_ledger.reconcile()
        assert report["残留"] == []
        assert report["丢失"] == []
        assert report["错位"] == []
        assert report["重复"] == []
        assert report["consistent"] is True

    def test_recovery_log_keeps_first_error_and_final_strategy(self) -> None:
        """失败恢复记录必须保存首次错误和最终采用策略。"""
        rule = add_enabled_rule("恢复记录验证元素", fallback="")
        ledger = LocatorLedgerService()
        dataset = ledger.build_dataset(rule_ids=[rule["id"]])
        rec = ledger.list_records(version=dataset["version"])[0]
        before = store.recovery.count
        ledger.register_receipt(
            rec["record_id"],
            {
                "success": False,
                "primary_success": False,
                "first_error": "选择器超时: 元素 #gone 在等待时间内未出现",
                "final_selector": None,
                "final_strategy": "无可用定位",
                "fallback_path": [{
                    "source": "主选择器", "selector_type": "css",
                    "expression": "#gone", "outcome": "失败",
                    "error": "选择器超时",
                }],
                "duration_ms": 120,
            },
            session_id="sess-recovery-test",
        )
        assert store.recovery.count == before + 1
        entry = store.recovery._entries[-1]
        assert "选择器超时" in entry.error_detail  # 首次错误
        assert "最终策略:无可用定位" in entry.step  # 最终采用策略


# ── 异常元素重测 ──

class TestRetest:
    def test_retest_generates_new_receipt_and_supersedes_old(self) -> None:
        """异常元素重新测试后生成新的执行回执，旧回执失效。"""
        rule = add_enabled_rule("重测验证元素")
        dataset = locator_ledger.build_dataset(rule_ids=[rule["id"]])
        runtime_session.consume_ledger_and_execute(version=dataset["version"])
        rec = locator_ledger.list_records(version=dataset["version"])[0]
        old_receipt = rec["receipt_id"]

        result = locator_ledger.retest_records([rec["record_id"]])
        assert result["retested"] == 1
        new_receipt_id = result["results"][0]["receipt_id"]
        assert new_receipt_id != old_receipt

        receipts = locator_ledger.list_receipts(record_id=rec["record_id"])
        assert len(receipts) == 2
        active = [r for r in receipts if not r["superseded"]]
        assert len(active) == 1  # 任一时刻只有一条有效回执
        assert active[0]["receipt_id"] == new_receipt_id

        # 重测后核对仍然一致
        report = locator_ledger.reconcile()
        assert report["consistent"] is True

    def test_retest_unknown_record_is_reported(self) -> None:
        result = locator_ledger.retest_records(["LER-9999"])
        assert result["retested"] == 0
        assert result["results"][0]["ok"] is False


# ── 兼容既有业务口径 ──

class TestSnapshotCompat:
    def test_ledger_snapshot_fields(self) -> None:
        snap = locator_ledger.domain_snapshot()
        for key in ("module", "entity", "artifact", "count",
                    "by_status", "abnormal"):
            assert key in snap
        assert snap["module"] == "locator_ledger"
        assert snap["artifact"] == "定位策略执行记录数据集"

    def test_runtime_session_snapshot_keeps_legacy_fields(self) -> None:
        snap = runtime_session.domain_snapshot()
        # 既有口径字段保留
        for key in ("module", "entity", "artifact", "active_sessions",
                    "actions_executed", "automation_events"):
            assert key in snap
        # 新增台账联动字段
        assert "receipts_generated" in snap
