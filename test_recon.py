import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ReconTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _sync(self, batch_id, side, rows):
        return self.store.sync_recon_list("staff", batch_id, side, rows)

    def _both_synced(self, stable_no="M-1", holder="本馆", circulated="1999-07-01", clues="购入"):
        batch = self.store.create_recon_batch("staff", "首批对账")
        self._sync(batch["id"], "A", [{"stable_no": stable_no, "holder": holder,
                                       "circulated_at": circulated, "clues": clues}])
        self._sync(batch["id"], "B", [{"stable_no": stable_no, "holder": "他馆",
                                       "circulated_at": circulated, "clues": "入藏"}])
        return batch["id"]

    def test_pair_by_stable_id_and_dual_confirm(self):
        self._both_synced()
        matches = self.store.list_recon_matches("reviewer1")
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual(m["stable_no"], "M-1")
        self.assertEqual(m["status"], "paired")
        # A 先确认
        r1 = self.store.confirm_recon_match("reviewer_a", m["id"], m["version"])
        self.assertEqual(r1["status"], "paired")
        self.assertIsNotNone(r1["confirmed_by_a"])
        self.assertIsNone(r1["confirmed_by_b"])
        # B 用旧版本同时提交 → 冲突
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_recon_match("reviewer_b", m["id"], m["version"])
        self.assertEqual(ctx.exception.code, "conflict")
        # B 刷新后确认 → 双方确认,状态 confirmed
        r2 = self.store.confirm_recon_match("reviewer_b", m["id"], r1["version"])
        self.assertEqual(r2["status"], "confirmed")
        self.assertIsNotNone(r2["confirmed_by_a"])
        self.assertIsNotNone(r2["confirmed_by_b"])
        # 交接只带结论
        handoff = self.store.recon_handoff("reviewer1")
        self.assertEqual(len(handoff["conclusions"]), 1)
        self.assertTrue(handoff["ready"])

    def test_pending_when_missing_side(self):
        batch = self.store.create_recon_batch("staff", "缺侧对账")
        self._sync(batch["id"], "A", [{"stable_no": "M-2", "holder": "本馆",
                                       "circulated_at": "2000-01-01", "clues": ""}])
        matches = self.store.list_recon_matches("reviewer1")
        self.assertEqual(matches[0]["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_recon_match("reviewer_a", matches[0]["id"], matches[0]["version"])
        self.assertEqual(ctx.exception.code, "match_incomplete")

    def test_discrepancy_requires_dual_confirm(self):
        self._both_synced("M-3")
        m = self.store.list_recon_matches("reviewer1")[0]
        self.store.mark_recon_discrepancy("reviewer_a", m["id"], "编号与藏品实际标签不一致")
        m = self.store.get_recon_match("reviewer1", m["id"])
        self.assertEqual(m["status"], "discrepancy")
        # 差异也需双方确认
        r1 = self.store.confirm_recon_match("reviewer_a", m["id"], m["version"])
        self.assertEqual(r1["status"], "discrepancy")
        r2 = self.store.confirm_recon_match("reviewer_b", m["id"], r1["version"])
        self.assertEqual(r2["status"], "confirmed")

    def test_record_change_invalidates_unconfirmed(self):
        self._both_synced("M-4")
        m = self.store.list_recon_matches("reviewer1")[0]
        self.store.confirm_recon_match("reviewer_a", m["id"], m["version"])
        # A 侧记录变化 → 作废未确认结果(B 尚未确认)
        self._sync(self.store.create_recon_batch("staff", "变更批次")["id"], "A",
                   [{"stable_no": "M-4", "holder": "本馆(改)", "circulated_at": "1999-07-01", "clues": "购入"}])
        m = self.store.get_recon_match("reviewer1", m["id"])
        self.assertEqual(m["status"], "pending")
        self.assertIsNone(m["confirmed_by_a"])
        self.assertIsNone(m["confirmed_by_b"])
        self.assertGreater(m["version"], 1)

    def test_confirmed_not_invalidated_and_skipped_on_retry(self):
        self._both_synced("M-5")
        m = self.store.list_recon_matches("reviewer1")[0]
        self.store.confirm_recon_match("reviewer_a", m["id"], m["version"])
        m = self.store.get_recon_match("reviewer1", m["id"])
        self.store.confirm_recon_match("reviewer_b", m["id"], m["version"])
        # 已确认后再同步,已核完行被跳过,结论不动
        res = self._sync(self.store.create_recon_batch("staff", "重试批次")["id"], "A",
                         [{"stable_no": "M-5", "holder": "本馆(新)", "circulated_at": "1999-07-01", "clues": "购入"}])
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(res["processed"], 0)
        m = self.store.get_recon_match("reviewer1", m["id"])
        self.assertEqual(m["status"], "confirmed")

    def test_claim_cannot_resolve_without_recon_confirm(self):
        obj = self.store.create_object("staff", "M-6", "青铜器", "礼器", "馆藏", "简介")
        claim = self.store.create_claim("claimant1", obj["id"], "王氏", "返还")
        self._both_synced("M-6")
        m = self.store.list_recon_matches("reviewer1")[0]
        # 未确认 → 不能推进返还
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全,进入调查")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接返还藏品")
        self.assertEqual(ctx.exception.code, "recon_not_confirmed")
        # 双方确认后可推进
        self.store.confirm_recon_match("reviewer_a", m["id"], m["version"])
        m = self.store.get_recon_match("reviewer1", m["id"])
        self.store.confirm_recon_match("reviewer_b", m["id"], m["version"])
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议")
        claim_data = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(claim_data["claims"][0]["status"], "resolved_return")

    def test_reviewer_without_side_cannot_confirm(self):
        self._both_synced("M-7")
        m = self.store.list_recon_matches("reviewer1")[0]
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_recon_match("reviewer1", m["id"], m["version"])
        self.assertEqual(ctx.exception.code, "reviewer_side_required")

    def test_retry_after_failure_only_fills_unfinished(self):
        batch = self.store.create_recon_batch("staff", "失败重试批次")
        # 先同步一条正常的
        self._sync(batch["id"], "A", [{"stable_no": "M-8", "holder": "本馆",
                                       "circulated_at": "1999-07-01", "clues": ""}])
        # 再同步一条非法的 → 批次失败
        with self.assertRaises(BusinessError):
            self._sync(batch["id"], "A", [{"stable_no": "", "holder": "x",
                                          "circulated_at": "1999-07-01", "clues": ""}])
        # 重试:已核完的行不碰,只补未核完行
        res = self._sync(batch["id"], "A", [{"stable_no": "M-8", "holder": "本馆",
                                             "circulated_at": "1999-07-01", "clues": ""},
                                            {"stable_no": "M-9", "holder": "本馆",
                                             "circulated_at": "2001-03-03", "clues": ""}])
        self.assertGreaterEqual(res["processed"], 1)
        matches = self.store.list_recon_matches("reviewer1")
        self.assertEqual(len(matches), 2)


if __name__ == "__main__":
    unittest.main()
