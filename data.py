"""Camada de dados: universo das mais líquidas (Fundamentus), históricos (Yahoo Finance) e modo demo."""
from __future__ import annotations

import re
import time
import zlib
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from io import StringIO

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}
COLS = ["Open", "High", "Low", "Close", "Volume"]

# Usada só se o Fundamentus estiver fora do ar (tickers inexistentes são ignorados no download).
FALLBACK_TICKERS = """PETR4 VALE3 ITUB4 BBDC4 BBAS3 PETR3 B3SA3 ABEV3 WEGE3 RENT3 ITSA4 SUZB3 BPAC11 JBSS3 ELET3 GGBR4
RDOR3 RADL3 EQTL3 PRIO3 LREN3 RAIL3 CSAN3 HAPV3 BBSE3 VIVT3 CMIG4 CPLE6 ENEV3 UGPA3 CSNA3 USIM5 MGLU3 COGN3
CVCB3 AZUL4 GOAU4 BRFS3 MRFG3 BEEF3 KLBN11 TOTS3 CYRE3 MRVE3 EMBR3 SBSP3 TAEE11 CPFE3 EGIE3 ENGI11 NATU3
VBBR3 BRKM5 ASAI3 CRFB3 HYPE3 MULT3 ALOS3 IRBR3 SANB11 BBDC3 ELET6 CMIN3 RECV3 RRRP3 VAMO3 SLCE3 SMTO3
YDUQ3 ANIM3 MOVI3 LWSA3 CASH3 PETZ3 SOMA3 ARZZ3 GMAT3 DXCO3 FLRY3 QUAL3 KEPL3 TIMS3 BRAP4 CXSE3 PSSA3
AURE3 AMOB3 BHIA3 EZTC3 CURY3 DIRR3 PLPL3 TEND3 ORVR3 BRAV3 MBLY3 AZEV4 OIBR3 STBP3""".split()


def _clean_ohlcv(d: pd.DataFrame | None, min_bars: int) -> pd.DataFrame | None:
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d = d.copy()
        d.columns = d.columns.get_level_values(-1) if "Open" in d.columns.get_level_values(-1) else d.columns.get_level_values(0)
    if not all(c in d.columns for c in COLS):
        return None
    d = d[COLS].apply(pd.to_numeric, errors="coerce").dropna()
    d = d[(d["High"] >= d["Low"]) & (d["Close"] > 0)]
    return d if len(d) >= min_bars else None


def _num_br(x) -> float:
    """'1.234.567,89' -> 1234567.89 (tolera número já convertido)."""
    if isinstance(x, (int, float)):
        return float(x)
    t = str(x).strip().replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return float("nan")


def get_universe(n: int = 100) -> tuple[list[str], str]:
    """Retorna (candidatos, fonte): os n com maior liquidez no Fundamentus + a lista de reserva (blue chips).
    O build_data.py baixa todos e FICA com os n de maior volume financeiro real (candidatos sem dados são ignorados)."""
    import requests
    try:
        r = requests.get("https://www.fundamentus.com.br/resultado.php", headers=HEADERS, timeout=30)
        r.raise_for_status()
        df = pd.read_html(StringIO(r.text), decimal=",", thousands=".", converters=None)[0]
        col_liq = next(c for c in df.columns if "liq" in str(c).lower())
        col_papel = next(c for c in df.columns if str(c).lower() == "papel")
        df = df[[col_papel, col_liq]].copy()
        df.columns = ["ticker", "liq"]
        df["liq"] = df["liq"].map(_num_br)
        df = df.dropna()
        df = df[(df["liq"] > 0) & df["ticker"].astype(str).str.match(r"^[A-Z]{4}\d{1,2}$")]
        df = df.sort_values("liq", ascending=False).head(n)
        if len(df) < 20:
            raise ValueError("poucos ativos")
        lista = [str(t).strip().upper() for t in df["ticker"]]
        return list(dict.fromkeys(lista + FALLBACK_TICKERS)), "fundamentus"
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] Fundamentus indisponível ({e}); usando lista de reserva.")
        return FALLBACK_TICKERS[:max(n, 60)], "reserva"


LAST_ERRORS: dict[str, str] = {}      # motivo da última falha por ativo (mostrado pelo build_data.py)
RATE_LIMIT = threading.Event()         # ligado quando o Yahoo diz "Too Many Requests": para de insistir


def _limitado(e: Exception) -> bool:
    t = f"{type(e).__name__} {e}".lower()
    return "ratelimit" in t or "too many requests" in t or "429" in t


def _tz_naive(d: pd.DataFrame) -> pd.DataFrame:
    if getattr(d.index, "tz", None) is not None:
        d = d.copy()
        d.index = d.index.tz_localize(None)
    return d


def _history(t: str, period: str):
    try:
        return yf.Ticker(t + ".SA").history(period=period, interval="1d", auto_adjust=False, timeout=15)
    except TypeError:                                   # versões antigas do yfinance não têm 'timeout'
        return yf.Ticker(t + ".SA").history(period=period, interval="1d", auto_adjust=False)


def _hist_ticker(t: str, period: str, min_bars: int):
    """Mesmo método que já funciona na aba Opções: Ticker().history. Falha RÁPIDO se o Yahoo estiver limitando."""
    if RATE_LIMIT.is_set():
        return None, "Yahoo limitou as requisições (Too Many Requests) — espere alguns minutos"
    motivo = "sem dados"
    for k in range(2):
        try:
            d = _clean_ohlcv(_history(t, period), min_bars)
            if d is not None:
                return _tz_naive(d), ""
            motivo = f"histórico vazio ou com menos de {min_bars} candles"
            break                                       # resposta vazia não melhora tentando de novo
        except Exception as e:  # noqa: BLE001
            motivo = f"{type(e).__name__}: {str(e)[:120]}"
            if _limitado(e):
                RATE_LIMIT.set()
                return None, "Yahoo limitou as requisições (Too Many Requests) — espere alguns minutos"
        time.sleep(1.0)
    return None, motivo


def download_daily(tickers: list[str], period: str = "5y", min_bars: int = 250,
                   max_seconds: float = 480.0) -> dict[str, pd.DataFrame]:
    """Candles diários (preços reais, sem ajuste), em paralelo, com progresso e tempo máximo."""
    if yf is None:
        raise RuntimeError("yfinance não instalado")
    out: dict[str, pd.DataFrame] = {}
    t0 = time.time()
    ex = ThreadPoolExecutor(max_workers=4)
    futs = {ex.submit(_hist_ticker, t, period, min_bars): t for t in tickers}
    feitos = 0
    try:
        for f in as_completed(futs, timeout=max_seconds):
            t = futs[f]
            feitos += 1
            d, motivo = f.result()
            if d is not None:
                out[t] = d
                LAST_ERRORS.pop(t, None)
            else:
                LAST_ERRORS[t] = motivo
            if feitos % 5 == 0 or feitos == len(tickers):
                print(f"    cotações: {feitos}/{len(tickers)} ({time.time() - t0:.0f}s)", flush=True)
    except TimeoutError:
        print(f"    [aviso] tempo máximo de {max_seconds:.0f}s para baixar cotações; seguindo com {len(out)} ativos.", flush=True)
        for f, t in futs.items():
            if t not in out:
                LAST_ERRORS.setdefault(t, "tempo esgotado")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if RATE_LIMIT.is_set():
        print("    [aviso] o Yahoo Finance está limitando as requisições deste computador/rede. "
              "Espere ~10 minutos e rode de novo (ou use os dados simulados).", flush=True)
    return out


def to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last",
                                     "Volume": "sum"}).dropna()


# ----------------------------------------------------------------------------
# Modo demo (dados SIMULADOS — apenas para testar o app sem internet)
# ----------------------------------------------------------------------------
def demo_frame(ticker: str, bars: int = 1250) -> pd.DataFrame:
    rng = np.random.default_rng(zlib.crc32(ticker.encode()))
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=bars)
    drift, vol, price, rows, left = 0.0, 0.016, rng.uniform(8, 60), [], 0
    for _ in idx:
        if left <= 0:
            drift = rng.choice([-0.0012, -0.0004, 0.0004, 0.0012, 0.0018]) + rng.normal(0, 0.0003)
            left = int(rng.integers(25, 90))
        left -= 1
        o = price * (1 + rng.normal(0, 0.004))
        c = o * (1 + drift + rng.normal(0, vol))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.006)))
        l = min(o, c) * (1 - abs(rng.normal(0, 0.006)))
        v = float(rng.lognormal(15, 0.45))
        rows.append((o, h, l, c, v))
        price = max(c, 1.0)
    return pd.DataFrame(rows, index=idx, columns=COLS)


def demo_universe(n: int) -> list[str]:
    return FALLBACK_TICKERS[:n]
