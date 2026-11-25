"""SQLite 存储与复核版本流。不依赖 Flask，可独立测试。

状态机（versions.status）：
  pending    待拟合（新数据或新复核版提交后）
  running    拟合中（此期间仍可提交新复核版）
  done       当前版拟合完成，可取数
  failed     拟合失败：复核决定保留在 exclusions，可请求重算
  superseded 被更新的复核版取代（永远不会成为当前版）

取数规则：只读 meta.current_version 指向的版本；
可比性按当前版剔除关系实时判断；效应值只取当前版 status='done' 的结果。
"""

import sqlite3
from datetime import datetime

MAX_SITES = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  site    TEXT NOT NULL,
  variety TEXT NOT NULL,
  grams   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_combo ON observations(site, variety);
CREATE TABLE IF NOT EXISTS versions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  status     TEXT NOT NULL
             CHECK(status IN ('pending','running','done','failed','superseded')),
  message    TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exclusions (
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  site       TEXT NOT NULL,
  PRIMARY KEY (version_id, site)
);
CREATE TABLE IF NOT EXISTS site_effects (
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  site       TEXT NOT NULL,
  effect     REAL NOT NULL,
  PRIMARY KEY (version_id, site)
);
CREATE TABLE IF NOT EXISTS variety_effects (
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  variety    TEXT NOT NULL,
  effect     REAL NOT NULL,
  PRIMARY KEY (version_id, variety)
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path):
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _rollback_quietly(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _insert_version(conn):
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO versions(status, created_at) VALUES ('pending', ?)", (now,))
    return cur.lastrowid


def _set_current(conn, vid):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('current_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(vid),))


def replace_dataset(db_path, rows):
    """整季数据替换：rows = [(site, variety, grams)]。

    全部写入在一个事务里；任何一步失败都回滚，沿用原状态。
    成功后产生复核版本 v1（无剔除，待拟合）。
    """
    if not rows:
        raise ValueError("CSV 中没有有效数据行")
    n_sites = len({r[0] for r in rows})
    if n_sites > MAX_SITES:
        raise ValueError(f"试验点数量 {n_sites} 超过上限 {MAX_SITES}")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("variety_effects", "site_effects", "exclusions",
                      "versions", "observations"):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            "INSERT INTO observations(site, variety, grams) VALUES (?,?,?)", rows)
        vid = _insert_version(conn)
        _set_current(conn, vid)
        conn.execute("COMMIT")
        return vid
    except Exception:
        _rollback_quietly(conn)
        raise
    finally:
        conn.close()


def submit_review(db_path, excluded_sites):
    """提交复核：以被剔除试验点集合生成新版本并置为当前版。

    事务失败则整体回滚，沿用原状态（原当前版不变）。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM meta WHERE key='current_version'").fetchone()
        if row is None:
            raise ValueError("尚未上传数据，无法复核")
        vid = _insert_version(conn)
        conn.executemany(
            "INSERT INTO exclusions(version_id, site) VALUES (?,?)",
            [(vid, s) for s in excluded_sites])
        _set_current(conn, vid)
        conn.execute("COMMIT")
        return vid
    except Exception:
        _rollback_quietly(conn)
        raise
    finally:
        conn.close()


def current_state(conn):
    """当前复核版 {id, status, message, created_at}；无数据时返回 None。"""
    row = conn.execute(
        "SELECT value FROM meta WHERE key='current_version'").fetchone()
    if row is None:
        return None
    v = conn.execute(
        "SELECT id, status, message, created_at FROM versions WHERE id=?",
        (int(row["value"]),)).fetchone()
    return dict(v) if v else None


def valid_combos(conn, vid):
    """当前版有效 (试验点, 品种) 组合：剔除点之外，按组合聚合计数与总和。"""
    rows = conn.execute(
        "SELECT o.site AS site, o.variety AS variety, "
        "       COUNT(*) AS c, SUM(o.grams) AS t "
        "FROM observations o "
        "WHERE o.site NOT IN (SELECT site FROM exclusions WHERE version_id=?) "
        "GROUP BY o.site, o.variety", (vid,)).fetchall()
    return [(r["site"], r["variety"], r["c"], r["t"]) for r in rows]


def excluded_sites(conn, vid):
    return {r["site"] for r in conn.execute(
        "SELECT site FROM exclusions WHERE version_id=?", (vid,))}


def dataset_stats(conn):
    n_obs = conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
    sites = conn.execute(
        "SELECT site, COUNT(*) AS n, COUNT(DISTINCT variety) AS nv "
        "FROM observations GROUP BY site ORDER BY site").fetchall()
    varieties = [r["variety"] for r in conn.execute(
        "SELECT DISTINCT variety FROM observations ORDER BY variety")]
    return n_obs, sites, varieties


def get_variety_effects(conn, vid):
    return {r["variety"]: r["effect"] for r in conn.execute(
        "SELECT variety, effect FROM variety_effects WHERE version_id=?", (vid,))}


def get_site_effects(conn, vid):
    return {r["site"]: r["effect"] for r in conn.execute(
        "SELECT site, effect FROM site_effects WHERE version_id=?", (vid,))}


def recent_versions(conn, limit=10):
    return conn.execute(
        "SELECT v.id, v.status, v.message, v.created_at, "
        "       (SELECT COUNT(*) FROM exclusions e WHERE e.version_id=v.id) AS nex "
        "FROM versions v ORDER BY v.id DESC LIMIT ?", (limit,)).fetchall()


def request_recompute(db_path):
    """拟合失败后请求重算：当前版 failed -> pending。返回是否成功置位。"""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE versions SET status='pending', message=NULL "
            "WHERE status='failed' AND id=(SELECT CAST(value AS INTEGER) FROM meta "
            "                             WHERE key='current_version')")
        return cur.rowcount == 1
    finally:
        conn.close()


def run_pending_fit_once(db_path, solver=None):
    """拟合worker的单次迭代：取最新待拟合版本执行一次。

    - 认领时把更早的 pending 版本标记为 superseded（只拟合最新关系）；
    - 拟合期间新提交的复核版不受影响，下一轮再处理；
    - 提交结果前若版本已被整季替换删除，则丢弃结果；
    - 拟合抛错：版本标记 failed，复核决定（exclusions）原样保留。
    返回处理的版本号；无待办返回 None。
    """
    if solver is None:
        from fit import solve_trial
        solver = solve_trial
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM versions WHERE status='pending' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        vid = row["id"]
        cur = conn.execute(
            "UPDATE versions SET status='running' WHERE id=? AND status='pending'",
            (vid,))
        if cur.rowcount == 0:
            return None
        conn.execute(
            "UPDATE versions SET status='superseded' "
            "WHERE status='pending' AND id<?", (vid,))

        try:
            combos = valid_combos(conn, vid)
            site_eff, var_eff = solver(combos)
        except Exception as exc:  # 拟合失败：保留复核决定，提示重算
            conn.execute(
                "UPDATE versions SET status='failed', message=? WHERE id=?",
                (str(exc) or exc.__class__.__name__, vid))
            return vid

        conn.execute("BEGIN IMMEDIATE")
        try:
            exists = conn.execute(
                "SELECT 1 FROM versions WHERE id=?", (vid,)).fetchone()
            if exists is not None:
                conn.execute(
                    "DELETE FROM site_effects WHERE version_id=?", (vid,))
                conn.execute(
                    "DELETE FROM variety_effects WHERE version_id=?", (vid,))
                conn.executemany(
                    "INSERT INTO site_effects(version_id, site, effect) "
                    "VALUES (?,?,?)",
                    [(vid, s, e) for s, e in site_eff.items()])
                conn.executemany(
                    "INSERT INTO variety_effects(version_id, variety, effect) "
                    "VALUES (?,?,?)",
                    [(vid, v, e) for v, e in var_eff.items()])
                conn.execute(
                    "UPDATE versions SET status='done', message=NULL WHERE id=?",
                    (vid,))
            conn.execute("COMMIT")
        except Exception:
            _rollback_quietly(conn)
            raise
        return vid
    finally:
        conn.close()
