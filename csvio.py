"""上传 CSV / 更正 CSV 解析。

上传（整季）：试验点、品种、实收克数，可选 季号、小区记录号。
更正（按试验点）：季号、小区记录号、所见克数、新克数。
"""

import csv
import io
import math

SITE_HEADERS = {"试验点", "试点", "点位", "地点", "site", "location"}
VARIETY_HEADERS = {"品种", "variety", "cultivar"}
GRAMS_HEADERS = {"实收克数", "克数", "产量", "实收", "grams", "gram",
                 "weight", "yield", "harvest"}
SEASON_HEADERS = {"季号", "季节", "season"}
RECORD_HEADERS = {"小区记录号", "记录号", "小区号", "小区编号",
                  "plot_record", "record", "plot", "plot_no", "plot_id"}
SEEN_HEADERS = {"所见克数", "所见", "seen", "seen_grams"}
NEW_HEADERS = {"新克数", "更正克数", "更正后克数", "new", "new_grams",
               "corrected", "corrected_grams"}


def _find(header, names):
    for i, h in enumerate(header):
        if h in names:
            return i
    return None


def _read_rows(text):
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    return [r for r in reader if any(cell.strip() for cell in r)]


def _parse_number(raw):
    """返回 (value, error)。空/非数字/非有限/负数都算无效。"""
    s = (raw or "").strip()
    if not s:
        return None, "克数无效（为空）"
    try:
        value = float(s)
    except ValueError:
        return None, f"克数无效（不是数字：{s!r}）"
    if not math.isfinite(value) or value < 0:
        return None, f"克数无效（{s!r}）"
    return value, None


def parse_upload_csv(text):
    """解析整季上传 CSV，返回 dict 行：
    season / plot_record / site / variety / grams。失败抛 ValueError。

    季号、小区记录号列可省略；省略时季号为空，记录号按文件行号补齐。
    两种列都存在时按原值保存。
    """
    rows = _read_rows(text)
    if not rows:
        raise ValueError("CSV 为空")
    header = [h.strip().lower() for h in rows[0]]
    si = _find(header, SITE_HEADERS)
    vi = _find(header, VARIETY_HEADERS)
    gi = _find(header, GRAMS_HEADERS)
    if si is None or vi is None or gi is None:
        raise ValueError(
            "表头需包含 试验点 / 品种 / 实收克数（或 site / variety / grams），"
            f"实际表头：{rows[0]}")
    seasi = _find(header, SEASON_HEADERS)
    ri = _find(header, RECORD_HEADERS)
    need = max(x for x in (si, vi, gi, seasi, ri) if x is not None)
    out = []
    for ln, row in enumerate(rows[1:], start=2):
        if len(row) <= need:
            raise ValueError(f"第 {ln} 行列数不足")
        site = row[si].strip()
        variety = row[vi].strip()
        if not site or not variety:
            raise ValueError(f"第 {ln} 行试验点或品种为空")
        grams, err = _parse_number(row[gi])
        if err:
            raise ValueError(f"第 {ln} 行{err}")
        season = row[seasi].strip() if seasi is not None else ""
        record = row[ri].strip() if ri is not None else ""
        if not record:
            record = str(ln)
        out.append({"season": season, "plot_record": record, "site": site,
                    "variety": variety, "grams": grams})
    if not out:
        raise ValueError("CSV 中没有有效数据行")
    seasons = {r["season"] for r in out if r["season"]}
    if len(seasons) > 1:
        raise ValueError("一次只能上传一个季号，实际包含："
                         + "、".join(sorted(seasons)))
    return out


def parse_csv(text):
    """兼容旧接口：返回 [(site, variety, grams)]。"""
    return [(r["site"], r["variety"], r["grams"])
            for r in parse_upload_csv(text)]


def parse_correction_csv(text):
    """解析更正 CSV，返回 (rows, errors)。

    rows: {line, season, plot_record, seen, new}
    errors: {line, message} —— 单行格式问题；表头问题仍抛 ValueError。
    整个文件都会检查完，便于一次标出所有问题行。
    """
    raw = _read_rows(text)
    if not raw:
        raise ValueError("CSV 为空")
    header = [h.strip().lower() for h in raw[0]]
    seasi = _find(header, SEASON_HEADERS)
    ri = _find(header, RECORD_HEADERS)
    seeni = _find(header, SEEN_HEADERS)
    newi = _find(header, NEW_HEADERS)
    if None in (seasi, ri, seeni, newi):
        raise ValueError(
            "表头需包含 季号 / 小区记录号 / 所见克数 / 新克数，"
            f"实际表头：{raw[0]}")
    need = max(seasi, ri, seeni, newi)
    rows, errors = [], []
    for ln, row in enumerate(raw[1:], start=2):
        if len(row) <= need:
            errors.append({"line": ln, "message": "列数不足"})
            continue
        season = row[seasi].strip()
        record = row[ri].strip()
        if not record:
            errors.append({"line": ln, "message": "小区记录号为空"})
            continue
        seen, err1 = _parse_number(row[seeni])
        new, err2 = _parse_number(row[newi])
        if err1 or err2:
            errors.append({"line": ln,
                           "message": "；".join(e for e in (err1, err2) if e)})
            continue
        rows.append({"line": ln, "season": season, "plot_record": record,
                     "seen": seen, "new": new})
    if not rows and not errors:
        raise ValueError("CSV 中没有有效数据行")
    return rows, errors
