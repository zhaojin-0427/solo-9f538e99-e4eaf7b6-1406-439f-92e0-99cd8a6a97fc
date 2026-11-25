"""上传 CSV 解析。每行：试验点、品种、等面积小区实收克数。"""

import csv
import io
import math

SITE_HEADERS = {"试验点", "试点", "点位", "地点", "site", "location"}
VARIETY_HEADERS = {"品种", "variety", "cultivar"}
GRAMS_HEADERS = {"实收克数", "克数", "产量", "实收", "grams", "gram",
                 "weight", "yield", "harvest"}


def _find(header, names):
    for i, h in enumerate(header):
        if h in names:
            return i
    return None


def parse_csv(text):
    """解析 CSV 文本，返回 [(site, variety, grams)]。失败抛 ValueError。"""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
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
    need = max(si, vi, gi)
    out = []
    for ln, row in enumerate(rows[1:], start=2):
        if len(row) <= need:
            raise ValueError(f"第 {ln} 行列数不足")
        site = row[si].strip()
        variety = row[vi].strip()
        gs = row[gi].strip()
        if not site or not variety:
            raise ValueError(f"第 {ln} 行试验点或品种为空")
        try:
            grams = float(gs)
        except ValueError:
            raise ValueError(f"第 {ln} 行克数不是数字：{gs!r}")
        if not math.isfinite(grams) or grams < 0:
            raise ValueError(f"第 {ln} 行克数无效：{gs!r}")
        out.append((site, variety, grams))
    if not out:
        raise ValueError("CSV 中没有有效数据行")
    return out
