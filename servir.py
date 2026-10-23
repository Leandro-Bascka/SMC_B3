"""Servidor local do site: abre o navegador e mostra os endereços (PC e celular). Uso: python servir.py [porta]"""
import http.server
import os
import socket
import socketserver
import sys
import threading
import webbrowser

AQUI = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(AQUI, "site")


def ip_local():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # não envia nada; só descobre a interface de rede usada
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=SITE, **k)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")     # sempre mostra os dados mais recentes
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    if not os.path.exists(os.path.join(SITE, "index.html")):
        print("ERRO: não achei site/index.html. Rode este arquivo de dentro da pasta Smc_B3.")
        return 1
    if not os.path.exists(os.path.join(SITE, "data", "meta.json")):
        print("AVISO: ainda não há dados em site/data. O site abre, mas vazio.")
        print("       Rode antes:  python build_data.py --n 30 --out site --demo")
        print()
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = None
    for p in range(porta, porta + 20):
        try:
            srv = socketserver.ThreadingTCPServer(("0.0.0.0", p), H)
            porta = p
            break
        except OSError:
            print(f"Porta {p} ocupada, tentando a próxima...")
    if srv is None:
        print("ERRO: nenhuma porta livre entre 8000 e 8019. Feche programas antigos e tente de novo.")
        return 1
    ip = ip_local()
    print("=" * 60)
    print(" SERVIDOR RODANDO — deixe esta janela ABERTA enquanto usa o site")
    print(f"  No PC ........ http://localhost:{porta}")
    if ip:
        print(f"  No celular ... http://{ip}:{porta}   (mesmo Wi-Fi do PC)")
    print("  Para parar: Ctrl+C")
    print("=" * 60)
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{porta}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
