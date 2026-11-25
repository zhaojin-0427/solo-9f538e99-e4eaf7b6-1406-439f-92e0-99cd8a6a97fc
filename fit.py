"""最小二乘拟合核心：产量 = 试验点效应 + 品种效应，每行（每个小区）等权。

纯计算模块，不依赖第三方库，也不碰数据库。

内存规模：O(有效点×品种的实际组合数 + 试验点数²)。
做法：按 (试验点, 品种) 组合聚合计数与总和；正规方程中品种块是对角阵，
先解析消去品种效应，得到“试验点数 × 试验点数”的约简方程组；
每个连通组钉扎一个试验点（效应置 0）使方程可解，再回代品种效应。
同一连通组内品种效应之差与钉扎选择无关。
"""


class FitError(Exception):
    """拟合数值失败。调用方应保留复核决定并提示重算。"""


def _gauss_solve(a, b):
    """部分主元高斯消元。a: n×n 系数矩阵（不修改入参语义，内部复制），b: 右端。"""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-9:
            raise FitError("正规方程奇异，无法求解")
        m[col], m[piv] = m[piv], m[col]
        d = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / d
            if f == 0.0:
                continue
            row_r, row_c = m[r], m[col]
            for c in range(col, n + 1):
                row_r[c] -= f * row_c[c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = m[i][n]
        row = m[i]
        for j in range(i + 1, n):
            s -= row[j] * x[j]
        x[i] = s / row[i]
    return x


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        p = self.parent
        if x not in p:
            p[x] = x
            return x
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _aggregate(combos):
    """combos: 可迭代 (site, variety, count, total)。重复组合自动合并。

    返回 (组合计数, 点计数, 点总和, 品种计数, 品种总和) 五个 dict。
    """
    combo_count = {}
    site_n, site_sum = {}, {}
    var_n, var_sum = {}, {}
    for site, variety, cnt, tot in combos:
        if cnt is None or cnt <= 0:
            continue
        key = (site, variety)
        combo_count[key] = combo_count.get(key, 0.0) + cnt
        site_n[site] = site_n.get(site, 0.0) + cnt
        site_sum[site] = site_sum.get(site, 0.0) + tot
        var_n[variety] = var_n.get(variety, 0.0) + cnt
        var_sum[variety] = var_sum.get(variety, 0.0) + tot
    return combo_count, site_n, site_sum, var_n, var_sum


def _union_find(combo_count):
    uf = _UnionFind()
    for site, variety in combo_count:
        uf.union(("site", site), ("variety", variety))
    return uf


def variety_components(combos):
    """按有效 (试验点, 品种) 组合求连通关系，返回 {品种: 组号}。

    两个品种同组号 = 同点种过，或经别的品种和有效试验点连通 = 可比较。
    组号按组内最小品种名排序，从 0 起，保证结果稳定。
    """
    combo_count, _, _, _, _ = _aggregate(combos)
    uf = _union_find(combo_count)
    groups = {}
    for _, variety in combo_count:
        groups.setdefault(uf.find(("variety", variety)), set()).add(variety)
    comp = {}
    for cid, members in enumerate(sorted(groups.values(), key=lambda vs: min(vs))):
        for v in members:
            comp[v] = cid
    return comp


def solve_trial(combos):
    """拟合 产量 = 试验点效应 + 品种效应。

    combos: 可迭代 (site, variety, count, total)，每行原始数据等权，
            聚合后的 count 即该组合的小区数。
    返回 (site_effects: dict, variety_effects: dict)。
    每个连通组内排序第一的试验点效应钉扎为 0。
    """
    combo_count, site_n, site_sum, var_n, var_sum = _aggregate(combos)
    sites = sorted(site_n)
    if not sites:
        return {}, {}
    sidx = {s: i for i, s in enumerate(sites)}

    # 每个品种出现的 (试验点下标, 组合小区数)
    var_sites = {}
    for (site, variety), cnt in combo_count.items():
        var_sites.setdefault(variety, []).append((sidx[site], cnt))

    n = len(sites)
    # 约简正规方程：M = A - B·C⁻¹·Bᵀ，r = a - B·C⁻¹·b
    # A = diag(点计数)，C = diag(品种计数)，B = 点×品种组合计数（稀疏，不整体存储）
    mat = [[0.0] * n for _ in range(n)]
    rhs = [0.0] * n
    for i, s in enumerate(sites):
        mat[i][i] = site_n[s]
        rhs[i] = site_sum[s]
    for v, entries in var_sites.items():
        mv = var_n[v]
        bv = var_sum[v]
        for i, ci in entries:
            rhs[i] -= ci * bv / mv
            row_i = mat[i]
            for j, cj in entries:
                row_i[j] -= ci * cj / mv

    # 逐连通组求解：钉扎组内第一个试验点为 0，解其余点
    uf = _union_find(combo_count)
    site_groups = {}
    for s in sites:
        site_groups.setdefault(uf.find(("site", s)), []).append(sidx[s])

    site_effect = [0.0] * n
    for group in site_groups.values():
        group.sort()
        free = group[1:]  # group[0] 钉扎为 0
        if not free:
            continue
        sub = [[mat[i][j] for j in free] for i in free]
        vec = [rhs[i] for i in free]
        sol = _gauss_solve(sub, vec)
        for k, i in enumerate(free):
            site_effect[i] = sol[k]

    # 回代品种效应：v = (品种总和 - Σ B_iv·点效应) / 品种计数
    var_effect = {}
    for v, entries in var_sites.items():
        acc = var_sum[v]
        for i, ci in entries:
            acc -= ci * site_effect[i]
        var_effect[v] = acc / var_n[v]

    site_effects = {s: site_effect[sidx[s]] for s in sites}
    return site_effects, var_effect
