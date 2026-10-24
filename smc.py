"""
Motor Smart Money Concepts (SMC) — baseado no material "Smart Money Concept Trading Strategy" (HowToTrade).

Implementa, a partir de candles OHLCV:
  1. Estrutura de mercado (topos/fundos, HH/HL/LH/LL) e tendência
  2. BOS (Break of Structure) e CHoCH (Change of Character)
  3. Order Blocks (OB) e Breaker Blocks
  4. Fair Value Gaps (FVG)
  5. Liquidez (topos/fundos iguais, varreduras / liquidity grabs)
  6. Suporte e Resistência (clusters de pivôs)
  7. Premium / Discount (range atual)
  8. Plano de trade (passos 1-3 do livro): tendência -> OB de alta probabilidade -> entrada, stop e alvo
  9. Score 0-100 de confluência, para compra e para venda

Nenhuma função aqui acessa a internet; tudo opera sobre DataFrames.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

TF_PARAMS = {
    "1d": {"swing": 4, "window": 320, "name": "diário", "max_age": 60},
    "1wk": {"swing": 3, "window": 220, "name": "semanal", "max_age": 26},
    "1h": {"swing": 5, "window": 400, "name": "60 min", "max_age": 120},
}

SL_BUFFER_ATR = 0.25      # folga do stop além da zona (em ATR)
MIN_RISK_ATR = 0.5        # risco mínimo (em ATR)
MIN_FVG_ATR = 0.15        # tamanho mínimo de um FVG (em ATR)


# ----------------------------------------------------------------------------
# Indicadores auxiliares (EMA/RSI/ADX reaproveitados do código original)
# ----------------------------------------------------------------------------
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    g = d.where(d > 0, 0.0).ewm(alpha=1 / n, adjust=False).mean()
    p = (-d.where(d < 0, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = g / p.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _adx(df: pd.DataFrame, n: int = 14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    mdi = 100 * mdm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, mdi


# ----------------------------------------------------------------------------
# 1. Pivôs, estrutura, BOS e CHoCH
# ----------------------------------------------------------------------------
def find_swings(df: pd.DataFrame, n: int) -> list[dict]:
    """Pivôs de topo/fundo confirmados após n candles (sem look-ahead)."""
    H, L = df["High"].values, df["Low"].values
    out = []
    for i in range(n, len(df) - n):
        hi = H[i]
        if hi >= H[i - n:i + n + 1].max() and (H[i - n:i] < hi).all():
            out.append({"i": i, "type": "H", "price": float(hi), "conf": i + n})
        lo = L[i]
        if lo <= L[i - n:i + n + 1].min() and (L[i - n:i] > lo).all():
            out.append({"i": i, "type": "L", "price": float(lo), "conf": i + n})
    out.sort(key=lambda s: (s["conf"], s["i"]))
    return out


def market_structure(df: pd.DataFrame, swings: list[dict]):
    """
    Percorre candle a candle. Um fechamento acima do último topo confirmado é
    BOS de alta (se a tendência já era de alta) ou CHoCH de alta (se era de baixa).
    Simetricamente para baixa. Retorna (tendência, eventos).
    """
    C = df["Close"].values
    by_conf: dict[int, list] = {}
    for s in swings:
        by_conf.setdefault(s["conf"], []).append(s)
    trend, ch, cl, ph, pl = 0, None, None, None, None
    events = []
    for k in range(len(C)):
        for s in by_conf.get(k, []):
            if s["type"] == "H":
                s["label"] = "H" if ph is None else ("HH" if s["price"] > ph else "LH")
                ph, ch = s["price"], s
            else:
                s["label"] = "L" if pl is None else ("LL" if s["price"] < pl else "HL")
                pl, cl = s["price"], s
        c = C[k]
        if ch is not None and c > ch["price"]:
            events.append({"kind": "CHOCH" if trend == -1 else "BOS", "dir": 1,
                           "level": ch["price"], "from_i": ch["i"], "i": k})
            trend, ch = 1, None
        if cl is not None and c < cl["price"]:
            events.append({"kind": "CHOCH" if trend == 1 else "BOS", "dir": -1,
                           "level": cl["price"], "from_i": cl["i"], "i": k})
            trend, cl = -1, None
    return trend, events


# ----------------------------------------------------------------------------
# 2. Order Blocks e Breaker Blocks
# ----------------------------------------------------------------------------
def order_blocks(df: pd.DataFrame, events: list[dict], atr: np.ndarray):
    """
    OB de alta  = última vela de baixa antes do movimento que rompeu a estrutura para cima.
    OB de baixa = última vela de alta antes do movimento que rompeu para baixo.
    OB quebrado (fechamento além da zona) vira Breaker Block com polaridade invertida.
    """
    O, H, L, C = (df[c].values for c in ("Open", "High", "Low", "Close"))
    N = len(C)
    obs, seen = [], set()
    for e in events:
        k, a = e["i"], e["from_i"]
        if e["dir"] == 1:
            m = a + int(np.argmin(L[a:k + 1]))
            lo_b, ob_i = max(a + 1, m - 3), None
            for j in range(m, lo_b - 1, -1):
                if C[j] < O[j]:
                    ob_i = j
                    break
        else:
            m = a + int(np.argmax(H[a:k + 1]))
            lo_b, ob_i = max(a + 1, m - 3), None
            for j in range(m, lo_b - 1, -1):
                if C[j] > O[j]:
                    ob_i = j
                    break
        if ob_i is None:
            ob_i = m
        key = (ob_i, e["dir"])
        if key in seen or ob_i >= k:
            continue
        seen.add(key)
        top, bottom = float(H[ob_i]), float(L[ob_i])
        if top - bottom > 2.5 * atr[ob_i]:            # vela gigante: usa só o corpo
            top, bottom = float(max(O[ob_i], C[ob_i])), float(min(O[ob_i], C[ob_i]))
        obs.append({"kind": "OB", "dir": e["dir"], "top": top, "bottom": bottom, "i": ob_i,
                    "created_i": k, "origin": e["kind"]})

    breakers = []
    for ob in obs:
        broken_i, touches = None, 0
        for j in range(ob["created_i"] + 1, N):
            if ob["dir"] == 1:
                if C[j] < ob["bottom"]:
                    broken_i = j
                    break
                if L[j] <= ob["top"]:
                    touches += 1
            else:
                if C[j] > ob["top"]:
                    broken_i = j
                    break
                if H[j] >= ob["bottom"]:
                    touches += 1
        ob["touches"] = touches
        ob["status"] = "quebrado" if broken_i is not None else ("testado" if touches else "ativo")
        if broken_i is None:
            continue
        bd = -ob["dir"]
        status, t = "ativo", 0
        for j in range(broken_i + 1, N):
            if bd == -1:
                if C[j] > ob["top"]:
                    status = "invalidado"
                    break
                if H[j] >= ob["bottom"]:
                    t += 1
            else:
                if C[j] < ob["bottom"]:
                    status = "invalidado"
                    break
                if L[j] <= ob["top"]:
                    t += 1
        if status != "invalidado":
            breakers.append({"kind": "BREAKER", "dir": bd, "top": ob["top"], "bottom": ob["bottom"],
                             "i": ob["i"], "created_i": broken_i, "origin": "BREAKER",
                             "touches": t, "status": "testado" if t else "ativo"})
    return obs, breakers


# ----------------------------------------------------------------------------
# 3. Fair Value Gaps
# ----------------------------------------------------------------------------
def fair_value_gaps(df: pd.DataFrame, atr: np.ndarray):
    H, L = df["High"].values, df["Low"].values
    N = len(H)
    out = []
    for i in range(2, N):
        if L[i] > H[i - 2] and (L[i] - H[i - 2]) >= MIN_FVG_ATR * atr[i]:
            top, bottom, d = float(L[i]), float(H[i - 2]), 1
        elif H[i] < L[i - 2] and (L[i - 2] - H[i]) >= MIN_FVG_ATR * atr[i]:
            top, bottom, d = float(L[i - 2]), float(H[i]), -1
        else:
            continue
        filled = False
        for j in range(i + 1, N):
            if d == 1:
                if L[j] <= bottom:
                    filled = True
                    break
                top = min(top, float(L[j]))           # preenchimento parcial
            else:
                if H[j] >= top:
                    filled = True
                    break
                bottom = max(bottom, float(H[j]))
        if not filled and top > bottom:
            out.append({"dir": d, "top": top, "bottom": bottom, "i": i - 1})
    return out


# ----------------------------------------------------------------------------
# 4. Liquidez
# ----------------------------------------------------------------------------
def _cluster_equal(points: list[dict], tol: float, H, L, kind: str):
    pts = sorted(points, key=lambda s: s["price"])
    groups: list[list[dict]] = []
    for s in pts:
        if groups and s["price"] - groups[-1][0]["price"] <= tol:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        last_i = max(x["i"] for x in g)
        level = max(x["price"] for x in g) if kind == "H" else min(x["price"] for x in g)
        after = H[last_i + 1:] if kind == "H" else L[last_i + 1:]
        taken = bool((after > level).any()) if kind == "H" else bool((after < level).any())
        if not taken:
            out.append({"level": float(level), "i0": min(x["i"] for x in g), "i1": last_i, "count": len(g)})
    return out


def liquidity(df: pd.DataFrame, swings: list[dict], atr_last: float):
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values
    N = len(C)
    tol = 0.3 * atr_last
    recent = [s for s in swings if s["i"] >= N - 160]
    eq_highs = _cluster_equal([s for s in recent if s["type"] == "H"], tol, H, L, "H")
    eq_lows = _cluster_equal([s for s in recent if s["type"] == "L"], tol, H, L, "L")

    sweeps = []
    for s in swings:
        c = s["conf"]
        if c >= N - 1:
            continue
        if s["type"] == "H":
            idx = np.nonzero(H[c + 1:] > s["price"])[0]
            if len(idx):
                j = c + 1 + int(idx[0])
                if C[j] < s["price"] and j >= N - 25:
                    sweeps.append({"i": j, "level": s["price"], "side": "BSL", "dir": -1, "from_i": s["i"]})
        else:
            idx = np.nonzero(L[c + 1:] < s["price"])[0]
            if len(idx):
                j = c + 1 + int(idx[0])
                if C[j] > s["price"] and j >= N - 25:
                    sweeps.append({"i": j, "level": s["price"], "side": "SSL", "dir": 1, "from_i": s["i"]})
    return eq_highs, eq_lows, sweeps


# ----------------------------------------------------------------------------
# 5. Suporte / Resistência e range (premium / discount)
# ----------------------------------------------------------------------------
def support_resistance(swings: list[dict], N: int, atr_last: float, price: float):
    pts = sorted((s["price"], s["i"]) for s in swings if s["i"] >= N - 300)
    groups: list[list] = []
    for p, i in pts:
        if groups and p - groups[-1][0][0] <= 0.6 * atr_last:
            groups[-1].append((p, i))
        else:
            groups.append([(p, i)])
    levels = []
    for g in groups:
        if len(g) >= 2:
            lv = float(np.mean([x[0] for x in g]))
            levels.append({"level": lv, "touches": len(g), "last_i": max(x[1] for x in g),
                           "kind": "suporte" if lv < price else "resistência"})
    above = sorted([l for l in levels if l["level"] > price], key=lambda l: l["level"])[:4]
    below = sorted([l for l in levels if l["level"] <= price], key=lambda l: -l["level"])[:4]
    return above + below


def dealing_range(df: pd.DataFrame, swings: list[dict]):
    H, L = df["High"].values, df["Low"].values
    sh = [s for s in swings if s["type"] == "H"]
    sl = [s for s in swings if s["type"] == "L"]
    if not sh or not sl:
        return None
    a, b = sh[-1], sl[-1]
    start = min(a["i"], b["i"])
    hi = float(H[start:].max())
    lo = float(L[start:].min())
    if hi <= lo:
        return None
    price = float(df["Close"].values[-1])
    pos = (price - lo) / (hi - lo)
    return {"high": hi, "low": lo, "eq": (hi + lo) / 2, "pos": pos, "start_i": start,
            "zone": "DESCONTO" if pos < 0.45 else ("PREMIUM" if pos > 0.55 else "EQUILÍBRIO")}


# ----------------------------------------------------------------------------
# 6. Plano de trade (passos do livro)
# ----------------------------------------------------------------------------
def _targets(side: int, entry: float, c: dict) -> list[dict]:
    """Níveis de liquidez/estrutura na direção do trade, do mais próximo ao mais distante."""
    atr, H, L, N = c["atr"], c["H"], c["L"], c["N"]
    cand = []
    for s in c["swings"]:
        if side == 1 and s["type"] == "H" and s["price"] > entry:
            if s["conf"] + 1 >= N or H[s["conf"] + 1:].max() <= s["price"]:
                cand.append((s["price"], "topo estrutural"))
        if side == -1 and s["type"] == "L" and s["price"] < entry:
            if s["conf"] + 1 >= N or L[s["conf"] + 1:].min() >= s["price"]:
                cand.append((s["price"], "fundo estrutural"))
    for lq in (c["eq_highs"] if side == 1 else c["eq_lows"]):
        if (side == 1 and lq["level"] > entry) or (side == -1 and lq["level"] < entry):
            cand.append((lq["level"], "liquidez (topos iguais)" if side == 1 else "liquidez (fundos iguais)"))
    for sr in c["sr"]:
        if (side == 1 and sr["level"] > entry) or (side == -1 and sr["level"] < entry):
            cand.append((sr["level"], "resistência" if side == 1 else "suporte"))
    rg = c["range"]
    if rg:
        if side == 1 and rg["high"] > entry:
            cand.append((rg["high"], "topo do range"))
        if side == -1 and rg["low"] < entry:
            cand.append((rg["low"], "fundo do range"))
    cand.sort(key=lambda x: x[0] * side)
    out = []
    for lv, lb in cand:
        if out and abs(lv - out[-1]["level"]) < 0.4 * atr:
            continue
        out.append({"level": float(lv), "label": lb})
    return out


def _overlap(a_top, a_bot, b_top, b_bot) -> bool:
    return a_bot <= b_top and b_bot <= a_top


def build_plan(side: int, c: dict):
    price, atr, N = c["price"], c["atr"], c["N"]
    cands = []
    for z in c["obs"] + c["breakers"]:
        if z["dir"] != side or z["status"] in ("quebrado", "invalidado"):
            continue
        if side == 1 and z["bottom"] < price:
            cands.append(z)
        elif side == -1 and z["top"] > price:
            cands.append(z)
    if not cands:                                  # plano B: FVG como zona
        for g in c["fvgs"]:
            if g["dir"] == side and ((side == 1 and g["bottom"] < price) or (side == -1 and g["top"] > price)):
                cands.append({"kind": "FVG", "dir": side, "top": g["top"], "bottom": g["bottom"], "i": g["i"],
                              "created_i": g["i"], "origin": "FVG", "status": "ativo", "touches": 0})
    if not cands:
        return None

    rg = c["range"]
    ranked = []
    for z in cands:
        fvg = any(g["dir"] == side and _overlap(z["top"], z["bottom"], g["top"], g["bottom"]) for g in c["fvgs"])
        sweep = any(s["dir"] == side and abs(s["level"] - (z["bottom"] if side == 1 else z["top"])) <= 3 * atr
                    for s in c["sweeps"])
        mid = (z["top"] + z["bottom"]) / 2
        disc = bool(rg and ((side == 1 and mid < rg["eq"]) or (side == -1 and mid > rg["eq"])))
        dist = (price - z["top"]) / atr if side == 1 else (z["bottom"] - price) / atr
        dist = max(dist, 0.0)
        q = (2 if z["status"] == "ativo" else 1) + 2 * fvg + (1 if z["origin"] == "CHOCH" else 0) \
            + disc + sweep - 0.35 * dist - (3 if z["kind"] == "BREAKER" else 0) - (1 if z["kind"] == "FVG" else 0)
        z["_fvg"], z["_sweep"], z["_disc"], z["_dist"] = fvg, sweep, disc, dist
        ranked.append((q, z))
    ranked.sort(key=lambda x: -x[0])

    # Ciclo de vida: zonas cujo trade JÁ terminou (stop ou alvo final atingido) saem da análise.
    for _, z in ranked:
        plan = _plan_for_zone(side, z, c)
        if plan is not None:
            return plan
    return None


def _plan_for_zone(side: int, z: dict, c: dict):
    """Monta o plano de uma zona e descobre em que fase ele está olhando os candles DEPOIS da formação da zona:
       aguardar / em_zona  -> a entrada ainda não aconteceu;  andamento -> entrada acionada, alvo final ainda não;
       None                -> o trade já terminou (stop ou alvo final atingido): descartado."""
    price, atr, N = c["price"], c["atr"], c["N"]
    H, L = c["H"], c["L"]
    ref = z["top"] if side == 1 else z["bottom"]               # nível em que a ordem limite é executada
    k0 = None
    for j in range(int(z.get("created_i", z["i"])) + 1, N - 1):  # a última vela ainda conta como "em zona"
        if (side == 1 and L[j] <= ref) or (side == -1 and H[j] >= ref):
            k0 = j
            break
    triggered = k0 is not None

    if triggered:
        entry = ref
    elif side == 1:
        entry = min(z["top"], price)
    else:
        entry = max(z["bottom"], price)
    if side == 1:
        sl = z["bottom"] - SL_BUFFER_ATR * atr
        if entry - sl < MIN_RISK_ATR * atr:
            sl = entry - MIN_RISK_ATR * atr
    else:
        sl = z["top"] + SL_BUFFER_ATR * atr
        if sl - entry < MIN_RISK_ATR * atr:
            sl = entry + MIN_RISK_ATR * atr
    risk = abs(entry - sl)

    tg = _targets(side, entry, c)
    for t in tg:
        t["rr"] = abs(t["level"] - entry) / risk
    tps = []
    t1 = next((t for t in tg if t["rr"] >= 1.0), None)
    if t1:
        tps.append(t1)
    t2 = next((t for t in tg if t["rr"] >= 2.0 and (not t1 or t["level"] != t1["level"])), None)
    if t2:
        tps.append(t2)
    t3 = next((t for t in reversed(tg) if t["rr"] >= 3.0 and all(t["level"] != x["level"] for x in tps)), None)
    if t3:
        tps.append(t3)
    tps.sort(key=lambda t: t["rr"])
    if not tps:                                    # sem estrutura à frente: projeção por múltiplos de R
        for m in (1.5, 3.0):
            tps.append({"level": float(entry + side * m * risk), "label": f"projeção {m:.1f}R", "rr": m})
    for k, t in enumerate(tps):
        t["name"] = f"Alvo {k + 1}"
    main_i = 1 if len(tps) > 1 else 0
    rr_main = tps[main_i]["rr"]

    hits, trig_i = [], None
    if triggered:
        trig_i = k0
        for j in range(k0, N):                      # stop: vale já na vela da entrada (critério conservador)
            if (side == 1 and L[j] <= sl) or (side == -1 and H[j] >= sl):
                return None                         # trade já foi stopado -> sai da análise
        for t in tps:
            if any((H[j] >= t["level"]) if side == 1 else (L[j] <= t["level"]) for j in range(k0 + 1, N)):
                hits.append(t["name"])
        if tps[main_i]["name"] in hits:
            return None                             # alvo final já atingido -> trade concluído, sai da análise
        if N - 1 - k0 > c["max_age"]:
            return None                             # trade aberto há tempo demais: deixou de ser uma leitura útil
        for t in tps:
            t["hit"] = t["name"] in hits

    passou = ""
    if not triggered:
        lvl = tps[main_i]["level"]
        j0 = int(z.get("created_i", z["i"])) + 1
        if j0 < N and ((side == 1 and H[j0:].max() >= lvl) or (side == -1 and L[j0:].min() <= lvl)):
            return None                 # o movimento já foi até o alvo final SEM tocar a entrada: oportunidade perdida
        if j0 < N and ((side == 1 and H[j0:].max() >= tps[0]["level"]) or (side == -1 and L[j0:].min() <= tps[0]["level"])):
            passou = " (o preço já passou do Alvo 1 sem tocar a entrada)"

    last_bull = c["C"][-1] > c["O"][-1]
    if triggered:
        state = "andamento"
        dt = _fmt_t(c["times"][k0])
        falta = [t["name"] for t in tps if t["name"] not in hits and t["rr"] <= rr_main + 1e-9]
        txt = f"EM ANDAMENTO — entrada acionada em {dt}"
        txt += (f"; {', '.join(hits)} atingido" + ("s" if len(hits) > 1 else "") + f"; falta {', '.join(falta)}"
                if hits else "; nenhum alvo atingido ainda")
    elif (side == 1 and price < sl) or (side == -1 and price > sl):
        return None
    elif z["bottom"] <= price <= z["top"] or (side == 1 and price <= z["top"] * 1.003 and price >= z["bottom"]):
        react = (last_bull if side == 1 else not last_bull)
        state = "em_zona"
        txt = "EM ZONA — candle de confirmação OK" if react else "EM ZONA — aguardar candle de confirmação"
    else:
        d = abs(price - (z["top"] if side == 1 else z["bottom"])) / price * 100
        state = "aguardar"
        txt = f"AGUARDAR {'RECUO' if side == 1 else 'SUBIDA'} — {d:.1f}% da zona{passou}"

    return {
        "side": side, "kind": z["kind"], "origin": z["origin"], "zone_status": z["status"],
        "top": z["top"], "bottom": z["bottom"], "i0": z["i"], "touches": z.get("touches", 0),
        "entry": float(entry), "sl": float(sl), "risk_pct": risk / entry * 100,
        "tps": tps, "rr": float(rr_main),
        "fvg_confluence": bool(z["_fvg"]), "sweep_confluence": bool(z["_sweep"]),
        "in_discount": bool(z["_disc"]), "dist_atr": float(z["_dist"]),
        "state": state, "state_txt": txt,
        "triggered": triggered, "trig_i": trig_i, "hits": hits,
    }


def _fmt_t(t) -> str:
    if isinstance(t, str) and len(t) >= 10 and t[4] == "-":
        return f"{t[8:10]}/{t[5:7]}"
    return str(t)


# ----------------------------------------------------------------------------
# 7. Score de confluência (0-100)
# ----------------------------------------------------------------------------
def score_side(s: int, c: dict, plan):
    items = []

    def add(name, pts, mx, note):
        items.append({"name": name, "pts": round(float(pts), 1), "max": mx, "note": note})

    up = s == 1
    trend, htf, N = c["trend"], c["htf_trend"], c["N"]

    if trend == s:
        add("Estrutura (tempo gráfico)", 20, 20, "topos e fundos ascendentes" if up else "topos e fundos descendentes")
    elif trend == 0:
        add("Estrutura (tempo gráfico)", 6, 20, "estrutura indefinida")
    else:
        add("Estrutura (tempo gráfico)", 0, 20, "estrutura contra o trade")

    if htf is None:
        add("Tempo maior", 3, 10, "sem dado de tempo maior")
    elif htf == s:
        add("Tempo maior", 10, 10, "tempo maior a favor")
    elif htf == 0:
        add("Tempo maior", 4, 10, "tempo maior indefinido")
    else:
        add("Tempo maior", 0, 10, "tempo maior contra")

    pts, note = 0, "sem rompimento recente a favor"
    if c["events"]:
        e = c["events"][-1]
        age = N - 1 - e["i"]
        if e["dir"] == s:
            if e["kind"] == "CHOCH" and age <= 30:
                pts, note = (10 if age <= 15 else 7), f"CHoCH há {age} candles"
            elif e["kind"] == "BOS" and age <= 20:
                pts, note = (8 if age <= 10 else 5), f"BOS há {age} candles"
    add("Rompimento (BOS/CHoCH)", pts, 10, note)

    rg = c["range"]
    if rg:
        p = rg["pos"] if up else 1 - rg["pos"]
        pts = 10 if p < 0.35 else 7 if p < 0.5 else 3 if p < 0.65 else 0
        add("Premium / Discount", pts, 10,
            f"preço em {rg['zone'].lower()} ({rg['pos'] * 100:.0f}% do range)")
    else:
        add("Premium / Discount", 0, 10, "range indefinido")

    if plan:
        d = plan["dist_atr"] if plan["state"] != "em_zona" else 0.0
        base = 7 if plan["zone_status"] == "ativo" else 5
        prox = 6 if d <= 0.5 else 4 if d <= 2 else 2 if d <= 5 else 0
        org = 2 if plan["origin"] == "CHOCH" else 0
        add("Zona (OB/Breaker)", min(15, base + prox + org), 15,
            f"{plan['kind']} {plan['zone_status']}, origem {plan['origin']}, a {d:.1f} ATR")
        add("Confluência FVG", 5 if plan["fvg_confluence"] else 0, 5,
            "FVG sobrepõe a zona" if plan["fvg_confluence"] else "sem FVG na zona")
    else:
        add("Zona (OB/Breaker)", 0, 15, "nenhuma zona válida")
        add("Confluência FVG", 0, 5, "—")

    sweep = any(x["dir"] == s and N - 1 - x["i"] <= 15 for x in c["sweeps"])
    liq_pts = 6 if sweep else 0
    tgt_liq = c["eq_highs"] if up else c["eq_lows"]
    price = c["price"]
    has_eq = any((l["level"] > price) if up else (l["level"] < price) for l in tgt_liq)
    liq_pts += 4 if has_eq else 0
    add("Liquidez", liq_pts, 10,
        ("varredura de liquidez recente; " if sweep else "") + ("pool de liquidez como alvo" if has_eq else "sem pool óbvio no alvo"))

    if plan:
        rr = plan["rr"]
        add("Risco/Retorno", 10 if rr >= 3 else 7 if rr >= 2 else 4 if rr >= 1.5 else 0, 10, f"R:R {rr:.1f}")
    else:
        add("Risco/Retorno", 0, 10, "—")

    cf, notes = 0, []
    if (c["close"] > c["ema200"]) == up:
        cf += 3
        notes.append("EMA200 a favor")
    v = c["vol_rel"]
    cf += 3 if v >= 1.2 else 2 if v >= 0.9 else 1 if v >= 0.7 else 0
    notes.append(f"vol {v:.2f}x")
    r = c["rsi"]
    if (up and 40 <= r <= 65) or (not up and 35 <= r <= 60):
        cf += 2
        notes.append(f"RSI {r:.0f}")
    if c["adx"] >= 20 and ((c["di_p"] > c["di_m"]) == up):
        cf += 2
        notes.append(f"ADX {c['adx']:.0f}")
    add("Confirmações", cf, 10, ", ".join(notes))

    total = sum(i["pts"] for i in items)
    if plan is None:
        total = min(total, 45)
    elif plan["state"] == "invalidado":
        total = min(total, 30)
    elif plan["state"] == "andamento":          # trade já em curso: não é mais uma entrada nova
        total = min(total, 45 if plan["hits"] else 62)
    return int(round(min(100, total))), items


def _signal(score: int) -> str:
    return "SETUP A+" if score >= 80 else "SETUP FORTE" if score >= 65 else "EM FORMAÇÃO" if score >= 50 else "SEM SETUP"


# ----------------------------------------------------------------------------
# 8. Análise completa
# ----------------------------------------------------------------------------
def _f(x, nd=2):
    if x is None:
        return None
    try:
        x = float(x)
    except Exception:
        return None
    return None if (math.isnan(x) or math.isinf(x)) else round(x, nd)


def _times(df: pd.DataFrame, tf: str):
    if tf == "1h":
        idx = df.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        secs = (idx - pd.Timestamp("1970-01-01")) // pd.Timedelta("1s")
        return [int(x) for x in secs]
    return list(pd.DatetimeIndex(df.index).strftime("%Y-%m-%d"))


def trend_only(df: pd.DataFrame, swing: int = 3) -> int:
    if df is None or len(df) < 30:
        return 0
    sw = find_swings(df, swing)
    return market_structure(df, sw)[0]


def analyze(df: pd.DataFrame, tf: str = "1d", htf_df: pd.DataFrame | None = None, ticker: str = "") -> dict:
    p = TF_PARAMS[tf]
    df = df.tail(p["window"] + 200).copy()
    N = len(df)
    if N < 80:
        raise ValueError("histórico insuficiente")

    df["ATR"] = _atr(df)
    df["EMA50"], df["EMA200"] = _ema(df["Close"], 50), _ema(df["Close"], 200)
    df["RSI"] = _rsi(df["Close"])
    df["ADX"], df["DIP"], df["DIM"] = _adx(df)
    df["VOLREL"] = df["Volume"] / df["Volume"].rolling(20).mean()

    atr = df["ATR"].values
    atr_last = float(atr[-1])
    price = float(df["Close"].values[-1])

    swings = find_swings(df, p["swing"])
    trend, events = market_structure(df, swings)
    obs, breakers = order_blocks(df, events, atr)
    fvgs = fair_value_gaps(df, atr)
    eq_h, eq_l, sweeps = liquidity(df, swings, atr_last)
    sr = support_resistance(swings, N, atr_last, price)
    rg = dealing_range(df, swings)
    htf_trend = trend_only(htf_df) if htf_df is not None else None

    last = df.iloc[-1]
    times = _times(df, tf)
    ctx = {
        "times": times, "max_age": p["max_age"], "N": N, "atr": atr_last, "price": price, "trend": trend, "htf_trend": htf_trend, "events": events,
        "obs": obs, "breakers": breakers, "fvgs": fvgs, "eq_highs": eq_h, "eq_lows": eq_l, "sweeps": sweeps,
        "sr": sr, "range": rg, "swings": swings,
        "H": df["High"].values, "L": df["Low"].values, "C": df["Close"].values, "O": df["Open"].values,
        "close": price, "ema200": float(last["EMA200"]),
        "vol_rel": float(last["VOLREL"]) if not math.isnan(last["VOLREL"]) else 1.0,
        "rsi": float(last["RSI"]) if not math.isnan(last["RSI"]) else 50.0,
        "adx": float(last["ADX"]) if not math.isnan(last["ADX"]) else 0.0,
        "di_p": float(last["DIP"]) if not math.isnan(last["DIP"]) else 0.0,
        "di_m": float(last["DIM"]) if not math.isnan(last["DIM"]) else 0.0,
    }

    plan_l, plan_s = build_plan(1, ctx), build_plan(-1, ctx)
    sc_l, br_l = score_side(1, ctx, plan_l)
    sc_s, br_s = score_side(-1, ctx, plan_s)

    if max(sc_l, sc_s) < 40:
        direction = 0
    elif sc_l > sc_s:
        direction = 1
    elif sc_s > sc_l:
        direction = -1
    else:
        direction = trend
    score = max(sc_l, sc_s) if direction else max(sc_l, sc_s)
    plan = plan_l if direction == 1 else plan_s if direction == -1 else None
    breakdown = br_l if direction == 1 else br_s if direction == -1 else (br_l if sc_l >= sc_s else br_s)
    state = plan["state"] if plan else "sem_plano"

    T = lambda i: times[max(0, min(N - 1, int(i)))]
    W = min(p["window"], N)
    off = N - W

    # ---- insights em português ----
    tname = p["name"]
    tl = {1: "ALTA", -1: "BAIXA", 0: "INDEFINIDA"}
    ins = []
    htf_txt = {None: "sem dado", 1: "alta", -1: "baixa", 0: "indefinida"}[htf_trend]
    ins.append(f"Tendência {tname}: {tl[trend]} · tempo maior: {htf_txt}.")
    if events:
        e = events[-1]
        age = N - 1 - e["i"]
        nm = "CHoCH" if e["kind"] == "CHOCH" else "BOS"
        ins.append(f"Último evento: {nm} de {'alta' if e['dir'] == 1 else 'baixa'} há {age} candles "
                   f"(rompeu {e['level']:.2f}).")
    if rg:
        ins.append(f"Preço em {rg['zone']} do range {rg['low']:.2f}–{rg['high']:.2f} "
                   f"({rg['pos'] * 100:.0f}%); equilíbrio em {rg['eq']:.2f}. "
                   + ("Compras ficam melhores em desconto." if rg["pos"] < 0.5 else "Vendas ficam melhores em premium."))
    if plan:
        sd = "compra" if plan["side"] == 1 else "venda"
        ins.append(f"Zona de {sd}: {plan['kind']} {plan['bottom']:.2f}–{plan['top']:.2f} "
                   f"({plan['zone_status']}, origem {plan['origin']})"
                   + (" · confluência com FVG" if plan["fvg_confluence"] else "")
                   + (" · liquidez varrida junto à zona" if plan["sweep_confluence"] else "") + ".")
        ins.append(f"Plano: entrada {plan['entry']:.2f} · stop {plan['sl']:.2f} ({plan['risk_pct']:.1f}% de risco) · "
                   f"alvos " + " / ".join(f"{t['level']:.2f}" for t in plan["tps"]) + f" · R:R {plan['rr']:.1f}.")
        ins.append(plan["state_txt"] + ".")
        if plan["rr"] < 1.5:
            ins.append("⚠ R:R abaixo de 1,5 — o book recomenda esperar uma zona melhor.")
        if plan["risk_pct"] > 8:
            ins.append(f"⚠ Stop largo ({plan['risk_pct']:.1f}%): reduza o tamanho da posição.")
        if plan["side"] == 1 and trend == -1 or plan["side"] == -1 and trend == 1:
            ins.append("⚠ Trade contra a estrutura atual: só faz sentido após um CHoCH confirmar.")
    else:
        ins.append("Sem zona de entrada válida agora — aguarde um novo BOS/CHoCH e a formação de um OB.")
    recent_sw = [s for s in sweeps if N - 1 - s["i"] <= 15]
    for s in recent_sw[-2:]:
        ins.append(("Varredura de liquidez vendedora (fundo rompido e recuperado) — sinal de alta"
                    if s["dir"] == 1 else "Varredura de liquidez compradora (topo rompido e rejeitado) — sinal de baixa")
                   + f" em {s['level']:.2f}, há {N - 1 - s['i']} candles.")
    if eq_h:
        ins.append(f"Liquidez compradora (topos iguais) acima: {', '.join(f'{x['level']:.2f}' for x in eq_h[-2:])}.")
    if eq_l:
        ins.append(f"Liquidez vendedora (fundos iguais) abaixo: {', '.join(f'{x['level']:.2f}' for x in eq_l[-2:])}.")

    # ---- gestão da posição / saída ----
    lows = [s for s in swings if s["type"] == "L" and s["price"] < price]
    highs = [s for s in swings if s["type"] == "H" and s["price"] > price]
    manage = {}
    if lows:
        manage["compra_stop_movel"] = _f(lows[-1]["price"])
    if highs:
        manage["venda_stop_movel"] = _f(highs[-1]["price"])
    mg = []
    if lows:
        mg.append(f"Se COMPRADO: stop móvel no último fundo (HL) {lows[-1]['price']:.2f}; saia se fechar abaixo "
                  f"ou se surgir CHoCH de baixa.")
    if highs:
        mg.append(f"Se VENDIDO: stop móvel no último topo (LH) {highs[-1]['price']:.2f}; saia se fechar acima "
                  f"ou se surgir CHoCH de alta.")
    if plan and plan["tps"]:
        if plan.get("hits"):
            mg.append(f"{', '.join(plan['hits'])} já atingido(s): não é entrada nova. Se estiver posicionado, "
                      f"mantenha o stop na entrada ({plan['entry']:.2f}) ou no stop móvel.")
        elif plan.get("triggered"):
            mg.append("Entrada já acionada: acompanhe o trade; não abra nova posição só por este plano.")
        else:
            mg.append(f"Realize parcial no Alvo 1 ({plan['tps'][0]['level']:.2f}) e mova o stop para a entrada.")
    manage["notas"] = mg

    # ---- serialização ----
    cand_out = []
    for i in range(off, N):
        r = df.iloc[i]
        cand_out.append({"time": times[i], "open": _f(r["Open"]), "high": _f(r["High"]),
                         "low": _f(r["Low"]), "close": _f(r["Close"]), "volume": int(r["Volume"])})

    def line(col):
        return [{"time": times[i], "value": _f(df[col].iloc[i])} for i in range(off, N) if not math.isnan(df[col].iloc[i])]

    active_obs = [o for o in obs if o["status"] != "quebrado" and o["i"] >= N - 260]
    active_obs = sorted(active_obs, key=lambda o: -o["created_i"])
    ob_out = []
    for d in (1, -1):
        ob_out += [o for o in active_obs if o["dir"] == d][:3]
    for b in breakers[-3:]:
        ob_out.append(b)

    last_p = float(df["Close"].values[-2]) if N > 1 else price
    out = {
        "ticker": ticker, "tf": tf, "tf_name": tname,
        "price": _f(price), "change_pct": _f((price / last_p - 1) * 100),
        "last_time": times[-1], "atr": _f(atr_last), "atr_pct": _f(atr_last / price * 100),
        "trend": trend, "trend_label": tl[trend], "htf_trend": htf_trend,
        "range": ({"high": _f(rg["high"]), "low": _f(rg["low"]), "eq": _f(rg["eq"]),
                   "pos": _f(rg["pos"] * 100, 0), "zone": rg["zone"], "t0": T(rg["start_i"])} if rg else None),
        "swings": [{"time": T(s["i"]), "price": _f(s["price"]), "type": s["type"], "label": s.get("label", "")}
                   for s in swings if s["i"] >= off][-24:],
        "events": [{"kind": e["kind"], "dir": e["dir"], "level": _f(e["level"]), "t0": T(e["from_i"]), "t1": T(e["i"])}
                   for e in events if e["i"] >= off][-8:],
        "obs": [{"kind": o["kind"], "dir": o["dir"], "top": _f(o["top"]), "bottom": _f(o["bottom"]),
                 "t0": T(o["i"]), "status": o["status"], "origin": o["origin"]} for o in ob_out],
        "fvgs": [{"dir": g["dir"], "top": _f(g["top"]), "bottom": _f(g["bottom"]), "t0": T(g["i"])}
                 for g in sorted(fvgs, key=lambda g: -g["i"])[:8] if g["i"] >= N - 200],
        "liquidity": {
            "eq_highs": [{"level": _f(x["level"]), "t0": T(x["i0"]), "t1": T(x["i1"]), "count": x["count"]} for x in eq_h[-3:]],
            "eq_lows": [{"level": _f(x["level"]), "t0": T(x["i0"]), "t1": T(x["i1"]), "count": x["count"]} for x in eq_l[-3:]],
            "sweeps": [{"time": T(s["i"]), "level": _f(s["level"]), "dir": s["dir"], "side": s["side"]} for s in sweeps[-4:]],
        },
        "sr": [{"level": _f(s["level"]), "touches": s["touches"], "kind": s["kind"]} for s in sr],
        "plan": None,
        "scores": {"compra": sc_l, "venda": sc_s},
        "breakdown": breakdown,
        "direction": direction,
        "direction_label": {1: "COMPRA", -1: "VENDA", 0: "NEUTRO"}[direction],
        "score": score, "signal": _signal(score), "state": state,
        "indicators": {"rsi": _f(ctx["rsi"], 1), "adx": _f(ctx["adx"], 1), "vol_rel": _f(ctx["vol_rel"]),
                       "ema50": _f(last["EMA50"]), "ema200": _f(last["EMA200"])},
        "insights": ins, "manage": manage,
        "candles": cand_out, "ema50": line("EMA50"), "ema200": line("EMA200"),
    }
    if plan:
        out["plan"] = {
            "side": plan["side"], "kind": plan["kind"], "origin": plan["origin"], "zone_status": plan["zone_status"],
            "top": _f(plan["top"]), "bottom": _f(plan["bottom"]), "t0": T(plan["i0"]),
            "entry": _f(plan["entry"]), "sl": _f(plan["sl"]), "risk_pct": _f(plan["risk_pct"]),
            "tps": [{"name": t["name"], "level": _f(t["level"]), "rr": _f(t["rr"], 1), "label": t["label"],
                     "hit": bool(t.get("hit"))} for t in plan["tps"]],
            "rr": _f(plan["rr"], 1), "fvg_confluence": plan["fvg_confluence"], "sweep_confluence": plan["sweep_confluence"],
            "in_discount": plan["in_discount"], "state": plan["state"], "state_txt": plan["state_txt"],
            "triggered": plan["triggered"], "trig_t": T(plan["trig_i"]) if plan["trig_i"] is not None else None,
            "hits": plan["hits"],
        }
    return out


def summary(a: dict) -> dict:
    """Versão enxuta para a tabela do scanner."""
    p = a["plan"]
    return {
        "ticker": a["ticker"], "price": a["price"], "change_pct": a["change_pct"],
        "direction": a["direction"], "direction_label": a["direction_label"],
        "score": a["score"], "signal": a["signal"], "state": a["state"],
        "state_txt": p["state_txt"] if p else "sem zona válida",
        "trend": a["trend"], "htf_trend": a["htf_trend"],
        "rr": p["rr"] if p else None, "entry": p["entry"] if p else None,
        "sl": p["sl"] if p else None, "tp1": p["tps"][0]["level"] if p and p["tps"] else None,
        "zone_pos": a["range"]["zone"] if a["range"] else None,
        "score_compra": a["scores"]["compra"], "score_venda": a["scores"]["venda"],
        "last_event": (a["events"][-1]["kind"] + (" ↑" if a["events"][-1]["dir"] == 1 else " ↓")) if a["events"] else None,
    }
