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


def new_rows(season="2026春"):
    """带季号/记录号的整季数据。"""
    return [
        {"season": season, "plot_record": "P1", "site": "S1",
         "variety": "V1", "grams": 100.0},
        {"season": season, "plot_record": "P2", "site": "S1",
         "variety": "V2", "grams": 120.0},
        {"season": season, "plot_record": "P3", "site": "S2",
         "variety": "V1", "grams": 140.0},
        {"season": season, "plot_record": "P4", "site": "S2",
         "variety": "V2", "grams": 180.0},
        {"season": season, "plot_record": "P5", "site": "S3",
         "variety": "V3", "grams": 200.0},
    ]


def correction(line, record, seen, new, season="2026春"):
    return {"line": line, "season": season, "plot_record": record,
            "seen": seen, "new": new}


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

    def test_upload_with_season_and_record(self):
        rows = csvio.parse_upload_csv(
            "季号,小区记录号,试验点,品种,实收克数\n"
            "2026春,A-1,S1,V1,100\n2026春,A-2,S1,V1,110\n")
        self.assertEqual(rows[0], {"season": "2026春", "plot_record": "A-1",
                                   "site": "S1", "variety": "V1",
                                   "grams": 100.0})
        self.assertEqual([r["plot_record"] for r in rows], ["A-1", "A-2"])

    def test_upload_multi_season_rejected(self):
        with self.assertRaises(ValueError):
            csvio.parse_upload_csv(
                "季号,试验点,品种,实收克数\n2026春,S1,V1,1\n2026秋,S1,V1,2\n")

    def test_upload_missing_record_autofilled(self):
        rows = csvio.parse_upload_csv("试验点,品种,实收克数\nS1,V1,1\nS1,V1,2\n")
        self.assertEqual([r["plot_record"] for r in rows], ["2", "3"])

    def test_correction_parse_and_errors(self):
        text = ("季号,小区记录号,所见克数,新克数\n"
                "2026春,A-1,100,105\n"
                "2026春,A-2,abc,110\n"
                "2026春,,100,120\n")
        rows, errors = csvio.parse_correction_csv(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["plot_record"], "A-1")
        self.assertEqual({e["line"] for e in errors}, {3, 4})

    def test_correction_header_required(self):
        with self.assertRaises(ValueError):
            csvio.parse_correction_csv("季号,小区记录号,克数\n2026春,A,1\n")


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

    # ---- 称重单更正 ----

    def _prepare(self):
        vid = store.replace_dataset(self.db, new_rows())
        store.run_pending_fit_once(self.db)
        self.assertEqual(self.state()["status"], "done")
        return vid

    def _combos(self, vid):
        conn = self.conn()
        try:
            return {c[0]: c for c in store.valid_combos(conn, vid)}
        finally:
            conn.close()

    def _obs(self, site):
        conn = self.conn()
        try:
            return {r["plot_record"]: dict(r)
                    for r in store.site_weighing_rows(conn, site)}
        finally:
            conn.close()

    def test_weighing_rows_keep_original(self):
        self._prepare()
        rows = self._obs("S1")
        self.assertEqual(rows["P1"]["original_grams"], 100.0)
        self.assertEqual(rows["P1"]["grams"], 100.0)
        self.assertEqual(rows["P1"]["variety"], "V1")
        self.assertEqual(rows["P1"]["season"], "2026春")

    def test_correction_on_valid_site_creates_pending_version(self):
        vid = self._prepare()
        res = store.apply_correction(
            self.db, "S1", [correction(2, "P1", 100.0, 130.0)])
        self.assertTrue(res["applied"])
        self.assertTrue(res["refit"])
        self.assertEqual(res["updated"], 1)
        self.assertNotEqual(res["version_id"], vid)
        st = self.state()
        self.assertEqual((st["id"], st["status"]), (res["version_id"], "pending"))
        # 原始克数保留，当前克数更新
        rows = self._obs("S1")
        self.assertEqual(rows["P1"]["original_grams"], 100.0)
        self.assertEqual(rows["P1"]["grams"], 130.0)
        # 新版快照已按最新克数、沿用原剔除名单（无剔除）
        conn = self.conn()
        try:
            s1 = {c[1]: c for c in store.valid_combos(conn, res["version_id"])
                  if c[0] == "S1"}
        finally:
            conn.close()
        self.assertEqual(s1["V1"][2], 1)
        self.assertAlmostEqual(s1["V1"][3], 130.0)
        self.assertAlmostEqual(s1["V2"][3], 120.0)
        store.run_pending_fit_once(self.db)
        conn = self.conn()
        try:
            ve = store.get_variety_effects(conn, res["version_id"])
            # S1: V1=130, V2=120；S2: V1=140, V2=180
            # 点效应 S2=(260-250)/2；品种差 = (130-120 + 140-180)/2 = -15
            self.assertAlmostEqual(ve["V1"] - ve["V2"], -15.0)
        finally:
            conn.close()

    def test_correction_on_excluded_site_keeps_current_fit(self):
        vid = self._prepare()
        vid2 = store.submit_review(self.db, ["S2"])
        store.run_pending_fit_once(self.db)
        self.assertEqual(self.state()["id"], vid2)
        effects_before = None
        conn = self.conn()
        try:
            effects_before = store.get_variety_effects(conn, vid2)
        finally:
            conn.close()
        res = store.apply_correction(
            self.db, "S2", [correction(2, "P3", 140.0, 999.0)])
        self.assertTrue(res["applied"])
        self.assertFalse(res["refit"])
        self.assertEqual(res["version_id"], vid2)
        st = self.state()
        self.assertEqual((st["id"], st["status"]), (vid2, "done"))
        conn = self.conn()
        try:
            self.assertEqual(store.get_variety_effects(conn, vid2), effects_before)
        finally:
            conn.close()
        # 恢复被剔除点：新版本取数用更正后的克数
        vid3 = store.submit_review(self.db, [])
        store.run_pending_fit_once(self.db)
        conn = self.conn()
        try:
            ve = store.get_variety_effects(conn, vid3)
            # 更正 P3（S2 的 V1）140 -> 999：S1 V1=100,V2=120；
            # S2 V1=999,V2=180。点效应 S2 = (1179 - 549.5 - 150) = 479.5；
            # 品种差 = ((1099-479.5) - (300-479.5))/2 = 399.5
            self.assertAlmostEqual(ve["V1"] - ve["V2"], 399.5)
        finally:
            conn.close()

    def test_correction_rejects_bad_rows_and_keeps_state(self):
        vid = self._prepare()
        conn = self.conn()
        try:
            before = store.dataset_stats(conn)[0]
        finally:
            conn.close()
        cases = [
            ("过期季号", [correction(2, "P1", 100.0, 130.0, season="2025秋")]),
            ("不属所选点", [correction(2, "P3", 140.0, 130.0)]),
            ("所见值已变", [correction(2, "P1", 101.0, 130.0)]),
            ("负克数", [correction(2, "P1", 100.0, -1.0)]),
            ("记录号重复", [correction(2, "P1", 100.0, 130.0),
                            correction(3, "P1", 100.0, 140.0)]),
            ("不存在记录", [correction(2, "NOPE", 100.0, 130.0)]),
            # 一行好一行坏 -> 整单退回，好的也不生效
            ("混合", [correction(2, "P1", 100.0, 130.0),
                      correction(3, "P4", 140.0, 130.0)]),
        ]
        for label, rows in cases:
            res = store.apply_correction(self.db, "S1", rows)
            self.assertFalse(res["applied"], label)
            self.assertTrue(res["errors"], label)
        st = self.state()
        self.assertEqual((st["id"], st["status"]), (vid, "done"))
        rows = self._obs("S1")
        self.assertEqual(rows["P1"]["grams"], 100.0)  # 未被改动
        conn = self.conn()
        try:
            self.assertEqual(store.dataset_stats(conn)[0], before)
        finally:
            conn.close()

    def test_correction_parse_errors_reject_whole_file(self):
        vid = self._prepare()
        res = store.apply_correction(
            self.db, "S1", [correction(2, "P1", 100.0, 130.0)],
            parse_errors=[{"line": 3, "message": "列数不足"}])
        self.assertFalse(res["applied"])
        self.assertEqual(self.state()["id"], vid)
        self.assertEqual(self._obs("S1")["P1"]["grams"], 100.0)

    def test_correction_unknown_site_rejected(self):
        vid = self._prepare()
        res = store.apply_correction(
            self.db, "NOPE", [correction(2, "P1", 100.0, 130.0)])
        self.assertFalse(res["applied"])
        self.assertEqual(self.state()["id"], vid)

    def test_correction_same_value_no_refit(self):
        vid = self._prepare()
        res = store.apply_correction(
            self.db, "S1", [correction(2, "P1", 100.0, 100.0)])
        self.assertTrue(res["applied"])
        self.assertFalse(res["refit"])
        self.assertEqual(res["version_id"], vid)
        self.assertEqual(self.state()["id"], vid)

    def test_refit_uses_only_version_snapshot(self):
        """拟合/比较不扫描原始小区表：删掉 observations 后仍能拟合和比较。"""
        vid = self._prepare()
        vid2 = store.submit_review(self.db, ["S3"])
        conn = self.conn()
        try:
            conn.execute("DELETE FROM observations")
        finally:
            conn.close()
        store.run_pending_fit_once(self.db)
        self.assertEqual(self.state()["status"], "done")
        conn = self.conn()
        try:
            combos = store.valid_combos(conn, vid2)
            self.assertEqual({c[0] for c in combos}, {"S1", "S2"})
            ve = store.get_variety_effects(conn, vid2)
            self.assertAlmostEqual(ve["V1"] - ve["V2"], -30.0)
        finally:
            conn.close()

    def test_migrates_old_volume(self):
        """旧 SQLite 卷：observations 只有旧列时原地升级并保留数据。"""
        import sqlite3
        db = make_db()
        try:
            conn = sqlite3.connect(db)
            conn.executescript(
                "DROP TABLE observations;"
                "CREATE TABLE observations ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " site TEXT NOT NULL, variety TEXT NOT NULL,"
                " grams REAL NOT NULL);"
                "INSERT INTO observations(site, variety, grams) "
                "VALUES ('S1','V1',100),('S1','V2',120);")
            conn.commit()
            conn.close()
            store.init_db(db)
            conn = store.connect(db)
            try:
                rows = store.site_weighing_rows(conn, "S1")
            finally:
                conn.close()
            self.assertEqual(len(rows), 2)
            self.assertEqual([r["original_grams"] for r in rows], [100.0, 120.0])
            self.assertEqual([r["grams"] for r in rows], [100.0, 120.0])
            self.assertTrue(all(r["plot_record"] for r in rows))
            self.assertEqual(len({r["plot_record"] for r in rows}), 2)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db + suffix)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
