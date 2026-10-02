import base64
import tempfile
import threading
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)

    # ------------------------------------------------------------------
    # 两馆对账台账
    # ------------------------------------------------------------------

    def _row_a(self, **over):
        row = {"stable_no": "OBJ-1", "client_row_id": "A-1", "local_ref": "A-REF-1",
               "holder": "王氏旧藏", "transfer_date": "1938-05-01"}
        row.update(over)
        return row

    def _row_b(self, **over):
        row = {"stable_no": "OBJ-1", "client_row_id": "B-7", "local_ref": "B-REF-9",
               "holder": "王氏旧藏", "transfer_date": "1938-05-01"}
        row.update(over)
        return row

    def _claim_for(self, stable_no):
        obj = self.store.create_object("staff", f"M-{stable_no}", "造像", "石刻", "甲馆", "对账中藏品。")
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品", stable_no)
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        return claim

    def test_pairing_by_stable_no_mismatch_discrepancy_and_handoff_blocked(self):
        self.store.sync_recon_entries("staff", "A", [self._row_a()], "batch-a-1")
        self.store.sync_recon_entries("staff", "B", [self._row_b()], "batch-b-1")
        pairs = self.store.list_pairs("reviewer1")
        self.assertEqual(len(pairs), 1)
        current = pairs[0]
        self.assertEqual(current["status"], "discrepancy")
        fields = {d["field"] for d in current["discrepancies"]}
        self.assertEqual(fields, {"local_ref"})
        self.assertEqual(current["confirmed_sides"], [])
        with self.assertRaises(BusinessError) as ctx:
            self.store.handoff("staff", "OBJ-1")
        self.assertEqual(ctx.exception.code, "recon_not_ready")

    def test_matched_pair_requires_dual_reviewer_confirmation(self):
        self.store.sync_recon_entries("staff", "A", [self._row_a()], "batch-a-1")
        self.store.sync_recon_entries("staff", "B", [self._row_b(local_ref="A-REF-1")], "batch-b-1")
        current = self.store.confirm_pair("reviewer1", "OBJ-1", "甲馆核对无误。")
        self.assertEqual(current["status"], "matched")
        self.assertEqual(current["confirmed_sides"], ["A"])
        self.assertFalse(current["confirmed_both"])
        # 甲馆审查员不能替乙馆确认
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_pair("reviewer1", "OBJ-1", "越权再确认。")
        self.assertEqual(ctx.exception.code, "confirmation_conflict")
        current = self.store.confirm_pair("reviewer2", "OBJ-1", "乙馆核对无误。")
        self.assertEqual(current["confirmed_sides"], ["A", "B"])
        self.assertTrue(current["confirmed_both"])
        handoff = self.store.handoff("reviewer1", "OBJ-1")
        self.assertEqual(handoff["entry_a"]["local_ref"], handoff["entry_b"]["local_ref"])

    def test_one_sided_pair_cannot_confirm_and_becomes_pair_when_counterparty_arrives(self):
        self.store.sync_recon_entries("staff", "A", [self._row_a()], "batch-a-1")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_pair("reviewer1", "OBJ-1", "只有甲馆记录。")
        self.assertEqual(ctx.exception.code, "one_sided_pair")
        self.store.sync_recon_entries("staff", "B", [self._row_b()], "batch-b-1")
        self.assertEqual(self.store.list_pairs("staff")[0]["status"], "discrepancy")

    def test_duplicate_confirmation_winner_takes_all(self):
        self.store.sync_recon_entries("staff", "A", [self._row_a()], "batch-a-1")
        self.store.sync_recon_entries("staff", "B", [self._row_b(local_ref="A-REF-1")], "batch-b-1")
        with self.store.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,side) VALUES(?,?,?,?)",
                [("revA1", "甲馆审查员甲", "reviewer", "A"),
                 ("revA2", "甲馆审查员乙", "reviewer", "A")],
            )
        barrier = threading.Barrier(2)
        results = []

        def confirm(reviewer):
            barrier.wait()
            try:
                self.store.confirm_pair(reviewer, "OBJ-1", f"{reviewer} 同时提交确认。")
                results.append("ok")
            except BusinessError as exc:
                results.append(exc.code)

        threads = [threading.Thread(target=confirm, args=("revA1",)),
                   threading.Thread(target=confirm, args=("revA2",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn("confirmation_conflict", results)

    def test_sync_retry_only_inserts_missing_rows(self):
        rows = [
            self._row_a(),
            self._row_a(stable_no="OBJ-2", client_row_id="A-2", local_ref="A-REF-2",
                        holder="李氏旧藏", transfer_date="1940-01-01"),
        ]
        first = self.store.sync_recon_entries("staff", "A", rows, "batch-a-1")
        self.assertEqual((first["inserted"], first["skipped"]), (2, 0))
        # 同一批次重试：内容哈希一致，全部跳过，不产生新版本也不重建
        retry = self.store.sync_recon_entries("staff", "A", rows, "batch-a-1")
        self.assertEqual((retry["inserted"], retry["skipped"]), (0, 2))
        self.assertEqual(retry["rebuilt"], [])
        detail = self.store.pair_detail("staff", "OBJ-1")
        self.assertEqual(len(detail["history"]), 1)

    def test_side_change_before_dual_confirm_voids_unconfirmed_results(self):
        self.store.sync_recon_entries("staff", "A", [self._row_a()], "batch-a-1")
        self.store.sync_recon_entries("staff", "B", [self._row_b()], "batch-b-1")
        self.store.confirm_pair("reviewer1", "OBJ-1", "甲馆确认差异已记录。")
        # 乙馆修正持有人 → 单侧记录变化，未双确认版本作废、确认失效
        self.store.sync_recon_entries("staff", "B",
                                      [self._row_b(holder="王氏家族信托")], "batch-b-2")
        detail = self.store.pair_detail("staff", "OBJ-1")
        previous = detail["history"][-2]
        self.assertEqual(detail["current"]["confirmed_sides"], [])
        self.assertTrue(any(c["voided"] for c in previous["confirmations"]))
        self.assertEqual(previous["state"], "superseded_voided")
        self.assertGreater(detail["current"]["pair_seq"], previous["pair_seq"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.handoff("staff", "OBJ-1")
        self.assertEqual(ctx.exception.code, "recon_not_ready")

    def test_change_after_dual_confirm_freezes_old_version_and_handoff_uses_current_only(self):
        self.store.sync_recon_entries("staff", "A", [self._row_a()], "batch-a-1")
        self.store.sync_recon_entries("staff", "B", [self._row_b(local_ref="A-REF-1")], "batch-b-1")
        self.store.confirm_pair("reviewer1", "OBJ-1", "甲馆确认。")
        self.store.confirm_pair("reviewer2", "OBJ-1", "乙馆确认。")
        self.assertTrue(self.store.handoff("staff", "OBJ-1")["confirmed_both"])
        # 甲馆补来新的流转日期 → 旧双确认版冻结，新版本必须重新双确认
        self.store.sync_recon_entries("staff", "A",
                                      [self._row_a(transfer_date="1938-06-02")], "batch-a-2")
        detail = self.store.pair_detail("staff", "OBJ-1")
        previous = detail["history"][-2]
        self.assertEqual(detail["current"]["status"], "discrepancy")
        self.assertEqual(detail["current"]["confirmed_sides"], [])
        self.assertGreater(detail["current"]["pair_seq"], previous["pair_seq"])
        self.assertEqual(previous["state"], "frozen")
        with self.assertRaises(BusinessError) as ctx:
            self.store.handoff("staff", "OBJ-1")
        self.assertEqual(ctx.exception.code, "recon_not_ready")

    def test_claim_cannot_advance_to_return_without_dual_confirmation(self):
        self.store.sync_recon_entries("staff", "A", [self._row_a()], "batch-a-1")
        self.store.sync_recon_entries("staff", "B", [self._row_b(local_ref="A-REF-1")], "batch-b-1")
        claim = self._claim_for("OBJ-1")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "单方确认就返还。")
        self.assertEqual(ctx.exception.code, "recon_not_ready")
        self.store.confirm_pair("reviewer1", "OBJ-1", "甲馆确认。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "仍缺乙馆确认。")
        self.assertEqual(ctx.exception.code, "recon_not_ready")
        self.store.confirm_pair("reviewer2", "OBJ-1", "乙馆确认。")
        result = self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "双方确认，签署返还协议。")
        self.assertEqual(result["status"], "resolved_return")

    def test_changed_record_invalidates_return_even_if_previous_version_was_confirmed(self):
        self.store.sync_recon_entries("staff", "A", [self._row_a()], "batch-a-1")
        self.store.sync_recon_entries("staff", "B", [self._row_b(local_ref="A-REF-1")], "batch-b-1")
        self.store.confirm_pair("reviewer1", "OBJ-1", "甲馆确认。")
        self.store.confirm_pair("reviewer2", "OBJ-1", "乙馆确认。")
        claim = self._claim_for("OBJ-1")
        # 一侧记录变化后，旧双确认结论冻结，交接不得沿用 → 返还被拦下
        self.store.sync_recon_entries("staff", "A", [self._row_a(holder="王氏家族")], "batch-a-2")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "拿旧结论返还。")
        self.assertEqual(ctx.exception.code, "recon_not_ready")
        self.store.confirm_pair("reviewer1", "OBJ-1", "新版甲馆确认。")
        self.store.confirm_pair("reviewer2", "OBJ-1", "新版乙馆确认。")
        result = self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "新版双方确认后返还。")
        self.assertEqual(result["status"], "resolved_return")


if __name__ == "__main__":
    unittest.main()
