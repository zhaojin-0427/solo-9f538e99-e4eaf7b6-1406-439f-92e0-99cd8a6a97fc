"""SQLite 存储与复核版本流。不依赖 Flask，可独立测试。

状态机（versions.status）：
  pending    待拟合（新数据或新复核版提交后）
  running    拟合中（此期间仍可提交新复核版）
  done       当前版拟合完成，可取数
  failed     拟合失败：复核决定保留在 exclusions，可请求重算
  superseded 被更新的复核版取代（永远不会成为当前版）

取数规则：只读 meta.current_version 指向的版本；
可比性按当前版剔除关系实时判断；效应值只取当前版 status='done' 的结果。

称重单更正：
  observations.original_grams 保留导入时的原始克数，grams 是当前克数，
  更正只改 grams。若更正涉及未被剔除的试验点，则复制当前剔除名单生成
  一个新复核版（pending），由后台按最新克数重算；若只更正被剔除点，
  当前拟合结果不受影响，保留原版本。
"""

import math
import sqlite3
from datetime import datetime

MAX_SITES = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  season         TEXT NOT NULL,
  plot_record    TEXT NOT NULL,
  site           TEXT NOT NULL,
  variety        TEXT NOT NULL,
  original_grams REAL NOT NULL,
  grams          REAL NOT NULL
);
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
CREATE TABLE IF NOT EXISTS version_plots (
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  site       TEXT NOT NULL,
  variety    TEXT NOT NULL,
  count      REAL NOT NULL,
  total      REAL NOT NULL,
  PRIMARY KEY (version_id, site, variety)
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

# 旧卷（只有 id/site/variety/grams）升级后建立。放到迁移之后执行，
# 这样先把 season/plot_record/original_grams 回填完，再建唯一索引。
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_record
  ON observations(site, season, plot_record);
CREATE INDEX IF NOT EXISTS idx_obs_combo ON observations(site, variety);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _migrate_observations(conn):
    """把旧版 observations 表原地升级，保留旧卷里的全部数据。

    旧表没有季号/小区记录号，用空季号和不重复的行 id 回填，使旧数据
    仍能下载称重单并按记录号更正。原克数回填为当前克数。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(observations)")}
    changed = False
    if "season" not in cols:
        conn.execute(
            "ALTER TABLE observations ADD COLUMN season TEXT NOT NULL DEFAULT ''")
        changed = True
    if "plot_record" not in cols:
        conn.execute(
            "ALTER TABLE observations ADD COLUMN plot_record TEXT NOT NULL "
            "DEFAULT ''")
        changed = True
    if "original_grams" not in cols:
        conn.execute(
            "ALTER TABLE observations ADD COLUMN original_grams REAL NOT NULL "
            "DEFAULT 0")
        conn.execute("UPDATE observations SET original_grams = grams")
    if changed:
        conn.execute(
            "UPDATE observations SET plot_record = CAST(id AS TEXT) "
            "WHERE plot_record = ''")


def _backfill_version_plots(conn):
    """旧卷里的既有版本没有组合快照，按当时的剔除名单补一次。

    只补完全没有快照的版本；这是升级时的一次性动作。之后拟合和比较
    只读快照，不再扫描原始小区表。
    """
    conn.execute(
        "INSERT INTO version_plots(version_id, site, variety, count, total) "
        "SELECT v.id, o.site, o.variety, COUNT(*), SUM(o.grams) "
        "FROM versions v JOIN observations o "
        "WHERE NOT EXISTS (SELECT 1 FROM version_plots vp "
        "                  WHERE vp.version_id = v.id) "
        "  AND o.site NOT IN (SELECT site FROM exclusions e "
        "                     WHERE e.version_id = v.id) "
        "GROUP BY v.id, o.site, o.variety")


def init_db(db_path):
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate_observations(conn)
            for stmt in INDEXES.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            _backfill_version_plots(conn)
            conn.execute("COMMIT")
        except Exception:
            _rollback_quietly(conn)
            raise
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


def _normalize_rows(rows):
    """统一三元组（旧格式）与 dict（新格式）为
    (season, plot_record, site, variety, original_grams, grams)。
    """
    out = []
    for i, r in enumerate(rows):
        if isinstance(r, dict):
            season = str(r.get("season") or "").strip()
            record = str(r.get("plot_record") or "").strip()
            site = str(r.get("site") or "").strip()
            variety = str(r.get("variety") or "").strip()
            grams = r.get("grams")
            where = f"第 {i + 1} 行数据"
        else:
            if len(r) != 3:
                raise ValueError(f"第 {i + 1} 行数据格式不正确")
            site, variety, grams = r
            site = str(site).strip()
            variety = str(variety).strip()
            season, record = "", str(i + 1)
            where = f"第 {i + 1} 行数据"
        try:
            grams = float(grams)
        except (TypeError, ValueError):
            raise ValueError(f"{where}克数不是数字")
        if not math.isfinite(grams) or grams < 0:
            raise ValueError(f"{where}克数无效")
        if not site or not variety:
            raise ValueError(f"{where}试验点或品种为空")
        if not record:
            raise ValueError(f"{where}小区记录号为空")
        out.append((season, record, site, variety, grams))
    return out


def _snapshot_version_plots(conn, vid, excluded_sites):
    """把当季未剔除点的小区合计写入该版本快照。

    这是把原始小区表折算进版本的一次性动作；之后可比性与拟合只读快照，
    不再扫描 observations。
    """
    excluded = list(excluded_sites)
    placeholders = ",".join("?" for _ in excluded)
    sql = (
        "INSERT INTO version_plots(version_id, site, variety, count, total) "
        "SELECT ?, o.site, o.variety, COUNT(*), SUM(o.grams) "
        "FROM observations o "
        + (f"WHERE o.site NOT IN ({placeholders}) " if excluded else "")
        + "GROUP BY o.site, o.variety")
    conn.execute(sql, [vid] + excluded)


def replace_dataset(db_path, rows):
    """整季数据替换：rows = [(site, variety, grams)] 或 dict 行。

    dict 行字段：season / plot_record / site / variety / grams。
    全部写入在一个事务里；任何一步失败都回滚，沿用原状态。
    成功后产生复核版本 v1（无剔除，待拟合），并快照全部组合。
    """
    rows = _normalize_rows(rows)
    if not rows:
        raise ValueError("CSV 中没有有效数据行")
    n_sites = len({r[2] for r in rows})
    if n_sites > MAX_SITES:
        raise ValueError(f"试验点数量 {n_sites} 超过上限 {MAX_SITES}")
    seen = {}
    for i, (season, record, site, _v, _g) in enumerate(rows, start=1):
        key = (site, season, record)
        if key in seen:
            raise ValueError(
                f"第 {i} 行小区记录号重复（与第 {seen[key]} 行相同）："
                f"{site} / 季号 {season or '（空）'} / {record}")
        seen[key] = i
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("version_plots", "variety_effects", "site_effects",
                      "exclusions", "versions", "observations"):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            "INSERT INTO observations"
            "(season, plot_record, site, variety, original_grams, grams) "
            "VALUES (?,?,?,?,?,?)",
            [(season, record, site, variety, grams, grams)
             for season, record, site, variety, grams in rows])
        vid = _insert_version(conn)
        _snapshot_version_plots(conn, vid, set())
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

    新版本快照当前未剔除点的小区合计（含最新的当前克数）。
    事务失败则整体回滚，沿用原状态（原当前版不变）。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM meta WHERE key='current_version'").fetchone()
        if row is None:
            raise ValueError("尚未上传数据，无法复核")
        excluded = set(excluded_sites)
        vid = _insert_version(conn)
        conn.executemany(
            "INSERT INTO exclusions(version_id, site) VALUES (?,?)",
            [(vid, s) for s in sorted(excluded)])
        _snapshot_version_plots(conn, vid, excluded)
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
    """当前版有效 (试验点, 品种) 组合：剔除点之外，按组合聚合计数与总和。

    组合来自该版本提交时快照的明细合计（version_plots，只统计未剔除点），
    不扫描原始小区表；更正后新版本会带上最新当前克数。
    """
    rows = conn.execute(
        "SELECT site, variety, count, total FROM version_plots "
        "WHERE version_id=? AND site NOT IN "
        "      (SELECT site FROM exclusions WHERE version_id=?)",
        (vid, vid)).fetchall()
    return [(r["site"], r["variety"], r["count"], r["total"]) for r in rows]


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


def site_weighing_rows(conn, site):
    """某试验点的称重单明细（含原始克数与当前克数）。"""
    return conn.execute(
        "SELECT id, season, plot_record, site, variety, "
        "       original_grams, grams "
        "FROM observations WHERE site=? "
        "ORDER BY season, plot_record, id", (site,)).fetchall()


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


def _fmt_grams(g):
    return format(float(g), ".12g")


def fmt_grams(g):
    """称重单展示/导出用的克数格式（去掉多余的 .0，保留有效数字）。"""
    return _fmt_grams(g)


def _same_grams(a, b):
    a, b = float(a), float(b)
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _sanitize_corrections(rows):
    """检查每行的季节/记录号/所见克数/新克数格式。

    返回 (clean, errors)。clean 行含 season/plot_record/seen/new/line。
    """
    clean, errors = [], []
    for i, r in enumerate(rows):
        line = r.get("line", i + 2)
        season = str(r.get("season") or "").strip()
        record = str(r.get("plot_record") or "").strip()
        if not record:
            errors.append({"line": line, "message": "小区记录号为空"})
            continue
        try:
            seen = float(r.get("seen"))
            new = float(r.get("new"))
        except (TypeError, ValueError):
            errors.append({"line": line, "message": "克数无效（不是数字）"})
            continue
        if not math.isfinite(seen) or seen < 0:
            errors.append({"line": line, "message": "所见克数无效"})
            continue
        if not math.isfinite(new) or new < 0:
            errors.append({"line": line, "message": "新克数无效"})
            continue
        clean.append({"line": line, "season": season,
                      "plot_record": record, "seen": seen, "new": new})
    return clean, errors


def _check_corrections(conn, site, rows):
    """在单内小区明细中校验更正行。返回 (errors, matched)。

    matched: [(更正行, 明细行)]，仅对完全通过校验的行给出。
    """
    errors, matched = [], []

    by_record = {}
    for r in rows:
        by_record.setdefault(r["plot_record"], []).append(r)
    duplicated = set()
    for record, items in by_record.items():
        if len(items) > 1:
            duplicated.add(record)
            lines = "、".join(str(x["line"]) for x in items)
            for r in items:
                errors.append({
                    "line": r["line"],
                    "message": f"小区记录号 {record} 在文件中重复（第 {lines} 行）"})

    details = conn.execute(
        "SELECT id, site, season, plot_record, variety, "
        "       original_grams, grams "
        "FROM observations WHERE site=?", (site,)).fetchall()
    by_detail = {}
    for o in details:
        by_detail.setdefault(o["plot_record"], []).append(o)

    for r in rows:
        if r["plot_record"] in duplicated:
            continue
        record = r["plot_record"]
        matches = by_detail.get(record)
        if not matches:
            errors.append({
                "line": r["line"],
                "message": f"小区记录号 {record} 不属于所选试验点 {site}"
                           f"（该点称重单中无此记录）"})
            continue

        exact = [o for o in matches if o["season"] == r["season"]]
        if not exact:
            current = "、".join(
                (o["season"] or "（空）") for o in matches)
            errors.append({
                "line": r["line"],
                "message": f"季号已过期：记录当前季号为 {current}，"
                           f"提交为 {r['season'] or '（空）'}"})
            continue
        if len(exact) > 1:
            errors.append({
                "line": r["line"],
                "message": f"小区记录号 {record} 在当季明细中重复"})
            continue
        o = exact[0]
        if not _same_grams(o["grams"], r["seen"]):
            errors.append({
                "line": r["line"],
                "message": f"所见克数已变：当前为 {_fmt_grams(o['grams'])} 克，"
                           f"提交为 {_fmt_grams(r['seen'])} 克"})
            continue
        matched.append((r, o))
    return errors, matched


def apply_correction(db_path, site, rows, parse_errors=()):
    """按所选试验点整单更正称重单。

    rows: [{line, season, plot_record, seen, new}]；parse_errors 为 CSV 解析
    阶段已发现的问题行。任何一行有问题则整单退回，不改数据、不改版本。
    全部通过后在一个事务里更新当前克数；原始克数保持不变。
    更正涉及有效（未剔除）试验点且值确有变化时，复制当前剔除名单生成
    新复核版供后台按最新克数重算；只更正被剔除点则保留当前拟合结果。

    返回 {applied, errors, updated, version_id, refit, changed}。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        clean, errors = _sanitize_corrections(rows)
        errors = [dict(e) for e in parse_errors] + errors

        site = (site or "").strip()
        if not site:
            errors.append({"line": 0, "message": "请选择试验点"})
        else:
            present = conn.execute(
                "SELECT 1 FROM observations WHERE site=? LIMIT 1",
                (site,)).fetchone()
            if present is None:
                errors.append({
                    "line": 0,
                    "message": f"试验点 {site} 不在当前数据中"})

        if not errors:
            check_errors, matched = _check_corrections(conn, site, clean)
            errors.extend(check_errors)
        else:
            matched = []

        # 行有问题：整单退回。ROLLBACK 同时撤销本轮可能的任何写入。
        if errors:
            _rollback_quietly(conn)
            errors.sort(key=lambda e: (e.get("line") or 0))
            return {"applied": False, "errors": errors,
                    "updated": 0, "version_id": None, "refit": False,
                    "changed": False}

        cur = conn.execute(
            "SELECT value FROM meta WHERE key='current_version'").fetchone()
        if cur is None:
            _rollback_quietly(conn)
            return {"applied": False,
                    "errors": [{"line": 0, "message": "尚未上传试验数据"}],
                    "updated": 0, "version_id": None, "refit": False,
                    "changed": False}
        cur_vid = int(cur["value"])
        excluded = excluded_sites(conn, cur_vid)

        changed = False
        deltas = {}
        for r, o in matched:
            if _same_grams(o["grams"], r["new"]):
                continue
            conn.execute(
                "UPDATE observations SET grams=? WHERE id=?", (r["new"], o["id"]))
            key = (o["site"], o["variety"])
            acc = deltas.setdefault(key, [0.0, 0.0])
            acc[0] += o["grams"]
            acc[1] += r["new"]
            changed = True

        refit = changed and site not in excluded
        vid = cur_vid
        if refit:
            vid = _insert_version(conn)
            conn.executemany(
                "INSERT INTO exclusions(version_id, site) VALUES (?,?)",
                [(vid, s) for s in sorted(excluded)])
            conn.execute(
                "INSERT INTO version_plots(version_id, site, variety, count, total) "
                "SELECT ?, site, variety, count, total FROM version_plots "
                "WHERE version_id=?", (vid, cur_vid))
            for (dsite, variety), (old_sum, new_sum) in deltas.items():
                conn.execute(
                    "UPDATE version_plots SET total = total - ? + ? "
                    "WHERE version_id=? AND site=? AND variety=?",
                    (old_sum, new_sum, vid, dsite, variety))
            _set_current(conn, vid)

        conn.execute("COMMIT")
        return {"applied": True, "errors": [], "updated": len(matched),
                "version_id": vid, "refit": refit, "changed": changed}
    except Exception:
        _rollback_quietly(conn)
        raise
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
