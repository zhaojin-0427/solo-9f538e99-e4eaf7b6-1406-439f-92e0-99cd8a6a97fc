"""核心逻辑测试：fit.py / store.py / csvio.py（不依赖 Flask）。

运行：python3 tests/test_core.py
"""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import csvio
import fit
import store


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # 让 sqlite 自己建，避免空文件干扰 WAL
    store.init_db(path)
    return path


def combos_from_obs(obs):
    agg = {}
    for s, v, g in obs:
        c, t = agg.get((s, v), (0, 0.0))
        agg[(s, v)] = (c + 1, t + g)
    return [(s, v, c, t) for (s, v), (c, t) in agg.items()]


class FitTest(unittest.TestCase):
    def test_known_solution(self):
        # 手算结果：钉扎 S1=0，S2=50，V1=95，V2=125
        combos = [("S1", "V1", 1, 100.0), ("S1", "V2", 1, 120.0),
                  ("S2", "V1", 1, 140.0), ("S2", "V2", 1, 180.0)]
        se, ve = fit.solve_trial(combos)
        self.assertAlmostEqual(se["S1"], 0.0)
        self.assertAlmostEqual(se["S2"], 50.0)
        self.assertAlmostEqual(ve["V1"], 95.0)
        self.assertAlmostEqual(ve["V2"], 125.0)
        self.assertAlmostEqual(ve["V1"] - ve["V2"], -30.0)

    def test_replicates_equal_weight(self):
        # 同点同品种多小区：等权即取平均
        combos = [("S1", "V1", 2, 200.0), ("S1", "V2", 1, 130.0)]
        se, ve = fit.solve_trial(combos)
        self.assertAlmostEqual(se["S1"], 0.0)
        self.assertAlmostEqual(ve["V1"], 100.0)
        self.assertAlmostEqual(ve["V2"], 130.0)

    def test_normal_equation_residuals(self):
        # 随机数据：验证每个点/品种的残差和为 0（最小二乘的充要条件）
        rng = random.Random(42)
        sites = [f"S{i}" for i in range(6)]
        varieties = [f"V{i}" for i in range(10)]
        obs = []
        for s in sites:
            for v in varieties:
                if rng.random() < 0.55:  # 随机缺组合
                    for _ in range(rng.randint(1, 3)):
                        obs.append((s, v, rng.uniform(500, 1500)))
        combos = combos_from_obs(obs)
        se, ve = fit.solve_trial(combos)
        res_site = {s: 0.0 for s in sites}
        res_var = {v: 0.0 for v in varieties}
        for s, v, g in obs:
            r = g - se[s] - ve[v]
            res_site[s] += r
            res_var[v] += r
        for s in sites:
            if any(o[0] == s for o in obs):
                self.assertAlmostEqual(res_site[s], 0.0, places=6)
        for v in varieties:
            if any(o[1] == v for o in obs):
                self.assertAlmostEqual(res_var[v], 0.0, places=6)

    def test_components_chain_and_isolated(self):
        combos = [("S1", "V1", 1, 1.0), ("S1", "V2", 1, 1.0),
                  ("S2", "V2", 1, 1.0), ("S2", "V3", 1, 1.0),
                  ("S3", "V4", 1, 1.0)]
        comp = fit.variety_components(combos)
        # V1 与 V3 经 V2、S1/S2 连通
        self.assertEqual(comp["V1"], comp["V3"])
        # V4 孤立，不可比
        self.assertNotEqual(comp["V1"], comp["V4"])

    def test_single_site_component(self):
        combos = [("S1", "V1", 2, 200.0), ("S1", "V2", 3, 450.0)]
        se, ve = fit.solve_trial(combos)
        self.assertAlmostEqual(se["S1"], 0.0)
        self.assertAlmostEqual(ve["V1"], 100.0)
        self.assertAlmostEqual(ve["V2"], 150.0)

    def test_empty(self):
        se, ve = fit.solve_trial([])
        self.assertEqual(se, {})
        self.assertEqual(ve, {})


class CsvTest(unittest.TestCase):
    def test_chinese_header(self):
        rows = csvio.parse_csv("试验点,品种,实收克数\nS1,V1,100\nS1,V1,110\n")
        self.assertEqual(rows, [("S1", "V1", 100.0), ("S1", "V1", 110.0)])

    def test_english_header_and_bom(self):
        rows = csvio.parse_csv("﻿site,variety,grams\nS1,V1,100\n")
        self.assertEqual(rows, [("S1", "V1", 100.0)])

    def test_bad_number(self):
        with self.assertRaises(ValueError):
            csvio.parse_csv("试验点,品种,实收克数\nS1,V1,abc\n")

    def test_missing_header(self):
        with self.assertRaises(ValueError):
            csvio.parse_csv("a,b,c\n1,2,3\n")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            csvio.parse_csv("试验点,品种,实收克数\nS1,V1,-5\n")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.rows = [("S1", "V1", 100.0), ("S1", "V2", 120.0),
                     ("S2", "V1", 140.0), ("S2", "V2", 180.0),
                     ("S3", "V3", 200.0)]

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass

    def conn(self):
        return store.connect(self.db)

    def state(self):
        conn = self.conn()
        try:
            return store.current_state(conn)
        finally:
            conn.close()

    def test_upload_fit_compare_flow(self):
        vid = store.replace_dataset(self.db, self.rows)
        st = self.state()
        self.assertEqual((st["id"], st["status"]), (vid, "pending"))

        store.run_pending_fit_once(self.db)
        self.assertEqual(self.state()["status"], "done")

        conn = self.conn()
        try:
            ve = store.get_variety_effects(conn, vid)
            self.assertAlmostEqual(ve["V1"] - ve["V2"], -30.0)
            # 可比性：V1-V2 连通，V1-V3 不连通
            comp = fit.variety_components(store.valid_combos(conn, vid))
            self.assertEqual(comp["V1"], comp["V2"])
            self.assertNotEqual(comp["V1"], comp["V3"])
        finally:
            conn.close()

    def test_review_submit_immediate_comparability(self):
        store.replace_dataset(self.db, self.rows)
        store.run_pending_fit_once(self.db)
        # 提交剔除 S2：新版本 pending，可比性立即按新关系
        vid2 = store.submit_review(self.db, ["S2"])
        st = self.state()
        self.assertEqual((st["id"], st["status"]), (vid2, "pending"))
        conn = self.conn()
        try:
            combos = store.valid_combos(conn, vid2)
            sites = {c[0] for c in combos}
            self.assertNotIn("S2", sites)
            # 新版未算完：效应表为空 -> 页面应显示“待校正”
            self.assertEqual(store.get_variety_effects(conn, vid2), {})
        finally:
            conn.close()
        # 拟合完成后按新关系出数：只剩 S1，V1=100, V2=120
        store.run_pending_fit_once(self.db)
        self.assertEqual(self.state()["status"], "done")
        conn = self.conn()
        try:
            ve = store.get_variety_effects(conn, vid2)
            self.assertAlmostEqual(ve["V1"] - ve["V2"], -20.0)
        finally:
            conn.close()

    def test_fit_failure_keeps_review_and_recompute(self):
        store.replace_dataset(self.db, self.rows)
        vid2 = store.submit_review(self.db, ["S2"])

        def boom(combos):
            raise fit.FitError("模拟数值失败")

        store.run_pending_fit_once(self.db, solver=boom)
        st = self.state()
        self.assertEqual(st["status"], "failed")
        self.assertIn("模拟数值失败", st["message"])
        # 复核决定保留
        conn = self.conn()
        try:
            self.assertEqual(store.excluded_sites(conn, vid2), {"S2"})
        finally:
            conn.close()
        # 重算
        self.assertTrue(store.request_recompute(self.db))
        self.assertEqual(self.state()["status"], "pending")
        store.run_pending_fit_once(self.db)
        self.assertEqual(self.state()["status"], "done")

    def test_submit_review_failure_rolls_back(self):
        vid1 = store.replace_dataset(self.db, self.rows)
        # None 触发 NOT NULL 约束失败 -> 整体回滚，沿用原状态
        with self.assertRaises(Exception):
            store.submit_review(self.db, ["S2", None])
        st = self.state()
        self.assertEqual(st["id"], vid1)
        conn = self.conn()
        try:
            self.assertEqual(len(store.recent_versions(conn)), 1)
        finally:
            conn.close()

    def test_replace_dataset_failure_keeps_state(self):
        vid1 = store.replace_dataset(self.db, self.rows)
        store.run_pending_fit_once(self.db)
        # 超过 64 个试验点 -> 拒绝且原状态不变
        big = [(f"S{i}", "V1", 1.0) for i in range(65)]
        with self.assertRaises(ValueError):
            store.replace_dataset(self.db, big)
        st = self.state()
        self.assertEqual((st["id"], st["status"]), (vid1, "done"))
        conn = self.conn()
        try:
            self.assertEqual(store.dataset_stats(conn)[0], len(self.rows))
        finally:
            conn.close()

    def test_superseded_version_not_fitted(self):
        store.replace_dataset(self.db, self.rows)
        store.submit_review(self.db, ["S2"])
        vid3 = store.submit_review(self.db, ["S3"])
        store.run_pending_fit_once(self.db)
        self.assertEqual(self.state()["id"], vid3)
        self.assertEqual(self.state()["status"], "done")
        conn = self.conn()
        try:
            v2 = conn.execute(
                "SELECT status FROM versions WHERE id=?", (vid3 - 1,)).fetchone()
            self.assertEqual(v2["status"], "superseded")
        finally:
            conn.close()

    def test_upload_resets_everything(self):
        store.replace_dataset(self.db, self.rows)
        store.submit_review(self.db, ["S2"])
        store.run_pending_fit_once(self.db)
        vid_new = store.replace_dataset(self.db, [("A", "X", 1.0)])
        st = self.state()
        self.assertEqual((st["id"], st["status"]), (vid_new, "pending"))
        conn = self.conn()
        try:
            self.assertEqual(store.excluded_sites(conn, vid_new), set())
            self.assertEqual(store.dataset_stats(conn)[0], 1)
        finally:
            conn.close()

    def test_exclude_all_sites(self):
        store.replace_dataset(self.db, self.rows)
        store.submit_review(self.db, ["S1", "S2", "S3"])
        store.run_pending_fit_once(self.db)
        self.assertEqual(self.state()["status"], "done")  # 空拟合不算失败
        conn = self.conn()
        try:
            comp = fit.variety_components(
                store.valid_combos(conn, self.state()["id"]))
            self.assertEqual(comp, {})  # 任何品种都不可比
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
