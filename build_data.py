"""
Gera os arquivos estáticos que o site lê:
  <out>/data/meta.json              hora da atualização, fonte, contagens
  <out>/data/scan_1d.json           ranking (diário)
  <out>/data/scan_1wk.json          ranking (semanal)
  <out>/data/<TICKER>_1d.json       análise completa (candles + overlays SMC + plano)
  <out>/data/<TICKER>_1wk.json

Uso:  python build_data.py --n 100 --out site            (dados reais)
      python build_data.py --n 100 --out site --demo     (dados SIMULADOS, sem internet)
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import data
import smc


def dump(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--out", default="site")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--min-ratio", type=float, default=0.5,
                    help="fração mínima de ativos analisados para gravar os dados (0.5 no GitHub; use 0.05 para testar no PC)")
    args = ap.parse_args()

    out = os.path.join(args.out, "data")
    os.makedirs(out, exist_ok=True)
    t0 = time.time()

    if args.demo:
        tickers, source = data.demo_universe(args.n), "demo"
    else:
        tickers, source = data.get_universe(args.n)
    print(f"Universo: {len(tickers)} ativos ({source})")

    scans = {"1d": [], "1wk": []}
    failed = []
    for b in range(0, len(tickers), args.batch):
        chunk = tickers[b:b + args.batch]
        print(f"Baixando cotações de {b + 1} a {b + len(chunk)} de {len(tickers)}...", flush=True)
        try:
            frames = {t: data.demo_frame(t) for t in chunk} if args.demo else data.download_daily(chunk)
        except Exception as e:  # noqa: BLE001
            print(f"  [ERRO] não consegui baixar cotações: {e}")
            if "yfinance" in str(e):
                print("         -> rode:  pip install -r requirements.txt")
            frames = {}
        for t in chunk:
            df = frames.get(t)
            if df is None:
                failed.append(t)
                if len(failed) <= 5:
                    print(f"  {t:<7} sem histórico — {data.LAST_ERRORS.get(t, 'motivo desconhecido')}")
                elif len(failed) == 6:
                    print("  ... (demais falhas omitidas; o motivo costuma ser o mesmo)")
                continue
            try:
                wk = data.to_weekly(df)
                a_d = smc.analyze(df, "1d", htf_df=wk, ticker=t)
                a_w = smc.analyze(wk, "1wk", htf_df=None, ticker=t)
            except Exception as e:  # noqa: BLE001
                failed.append(t)
                print(f"  {t:<7} erro na análise: {e}")
                continue
            for tf, a in (("1d", a_d), ("1wk", a_w)):
                dump(os.path.join(out, f"{t}_{tf}.json"), a)
                scans[tf].append(smc.summary(a))
            print(f"  {t:<7} {a_d['direction_label']:<7} score {a_d['score']:>3}  {a_d['state']}")
        time.sleep(0.5)

    if not scans["1d"]:
        print("\nERRO: nenhum ativo foi analisado, então NENHUM dado foi gravado.")
        if data.RATE_LIMIT.is_set():
            print("      MOTIVO: o Yahoo Finance está limitando as requisições (Too Many Requests) deste computador/rede.")
            print("      Espere uns 10 a 15 minutos sem rodar nada e tente de novo; ou teste com poucos ativos:  python build_data.py --n 20 --out site")
        else:
            print("      Causas comuns: sem internet, Yahoo Finance bloqueando, ou 'pip install -r requirements.txt' não rodou.")
        print("      Para só ver o app funcionando, use os dados simulados:  python build_data.py --n 30 --out site --demo")
        return 1

    if not args.demo and len(scans["1d"]) < args.min_ratio * len(tickers):
        print(f"ERRO: só {len(scans['1d'])}/{len(tickers)} ativos analisados (Yahoo instável?) — não publicar.")
        return 1

    for tf in scans:
        scans[tf].sort(key=lambda r: (-r["score"], r["ticker"]))
        dump(os.path.join(out, f"scan_{tf}.json"), scans[tf])

    now = datetime.now(timezone.utc)
    dump(os.path.join(out, "meta.json"), {
        "updated_at": now.isoformat(timespec="seconds"),
        "updated_brt": (now - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M"),
        "source": source, "demo": bool(args.demo),
        "requested": len(tickers), "analyzed": len(scans["1d"]), "failed": failed,
        "seconds": round(time.time() - t0, 1),
    })
    print(f"Concluído: {len(scans['1d'])} ativos em {time.time() - t0:.0f}s ({len(failed)} falhas).")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)          # não espera threads do Yahoo que eventualmente fiquem presas
