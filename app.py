"""Flask 网页层：上传 CSV、复核（整点剔除/恢复）、品种效应差比较。

业务与存储逻辑在 fit.py / store.py / csvio.py，本文件只做 HTTP 粘合。
后台线程持续执行待拟合版本；拟合期间复核提交不受影响。
"""

import os
import threading
import time

from flask import Flask, flash, redirect, render_template, request, url_for

import csvio
import fit
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "trial.db"))
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret")

STATUS_TEXT = {
    "pending": "待拟合",
    "running": "拟合中",
    "done": "已完成",
    "failed": "拟合失败",
    "superseded": "已被新版取代",
}

store.init_db(DB_PATH)


def _worker():
    while True:
        try:
            store.run_pending_fit_once(DB_PATH)
        except Exception:
            app.logger.exception("拟合worker异常")
        time.sleep(0.5)


if os.environ.get("DISABLE_WORKER") != "1":
    threading.Thread(target=_worker, daemon=True, name="fit-worker").start()


@app.get("/")
def index():
    conn = store.connect(DB_PATH)
    try:
        state = store.current_state(conn)
        n_obs, sites, varieties = store.dataset_stats(conn)
        excluded = store.excluded_sites(conn, state["id"]) if state else set()
        versions = store.recent_versions(conn)
    finally:
        conn.close()
    return render_template(
        "index.html", state=state, n_obs=n_obs, n_sites=len(sites),
        n_varieties=len(varieties), excluded=sorted(excluded),
        versions=versions, status_text=STATUS_TEXT)


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if f is None or f.filename == "":
        flash("请选择 CSV 文件", "error")
        return redirect(url_for("index"))
    try:
        rows = csvio.parse_csv(f.read().decode("utf-8-sig"))
        vid = store.replace_dataset(DB_PATH, rows)
        n_sites = len({r[0] for r in rows})
        flash(f"已导入 {len(rows)} 行、{n_sites} 个试验点；"
              f"复核版本 v{vid}，拟合排队中", "ok")
    except Exception as e:
        flash(f"导入失败，已沿用原状态：{e}", "error")
    return redirect(url_for("index"))


@app.get("/review")
def review_form():
    conn = store.connect(DB_PATH)
    try:
        state = store.current_state(conn)
        if state is None:
            flash("请先上传试验数据", "error")
            return redirect(url_for("index"))
        _, sites, _ = store.dataset_stats(conn)
        excluded = store.excluded_sites(conn, state["id"])
    finally:
        conn.close()
    return render_template("review.html", state=state, sites=sites,
                           excluded=excluded, status_text=STATUS_TEXT)


@app.post("/review")
def review_submit():
    selected = request.form.getlist("exclude")
    conn = store.connect(DB_PATH)
    try:
        _, sites, _ = store.dataset_stats(conn)
    finally:
        conn.close()
    known = {s["site"] for s in sites}
    if not set(selected) <= known:
        flash("保存失败，已沿用原状态：包含未知试验点", "error")
        return redirect(url_for("review_form"))
    try:
        vid = store.submit_review(DB_PATH, selected)
        flash(f"复核版本 v{vid} 已提交（剔除 {len(selected)} 点），"
              f"可比性即刻按新关系判断，拟合排队中", "ok")
    except Exception as e:
        flash(f"保存失败，已沿用原状态：{e}", "error")
    return redirect(url_for("review_form"))


@app.post("/recompute")
def recompute():
    if store.request_recompute(DB_PATH):
        flash("已重新排队拟合", "ok")
    else:
        flash("当前版本不在失败状态，无需重算", "error")
    return redirect(request.referrer or url_for("index"))


def _compare(conn, state, a, b):
    vid = state["id"]
    if a == b:
        return {"kind": "error", "text": "两次选择了同一品种，请重新选择"}
    combos = store.valid_combos(conn, vid)
    comp = fit.variety_components(combos)
    if comp.get(a) is None or comp.get(b) is None or comp[a] != comp[b]:
        return {"kind": "incomparable"}
    if state["status"] in ("pending", "running"):
        return {"kind": "pending"}
    if state["status"] == "failed":
        return {"kind": "failed", "message": state["message"]}
    effects = store.get_variety_effects(conn, vid)
    if a not in effects or b not in effects:
        return {"kind": "pending"}
    return {"kind": "ok", "ea": effects[a], "eb": effects[b],
            "diff": effects[a] - effects[b]}


@app.get("/compare")
def compare():
    a = request.args.get("a", "").strip()
    b = request.args.get("b", "").strip()
    conn = store.connect(DB_PATH)
    try:
        state = store.current_state(conn)
        _, _, varieties = store.dataset_stats(conn)
        result = _compare(conn, state, a, b) if (state and a and b) else None
    finally:
        conn.close()
    return render_template("compare.html", state=state, varieties=varieties,
                           a=a, b=b, result=result, status_text=STATUS_TEXT)


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
