# Smc_B3 — Scanner Smart Money Concept da B3

Site que roda no **computador e no celular**, hospedado de graça no **GitHub Pages** e **atualizado de hora em hora**
durante o pregão (seg–sex, 10h05 às 18h05, horário de Brasília).

Baseado no PDF *Smart Money Concept Trading Strategy* (HowToTrade) e no seu scanner de liquidez (Fundamentus + Yahoo Finance).

## O que o app faz

Para cada uma das 100 ações mais líquidas (ranking de liquidez do Fundamentus, como no seu código):

| Conceito do livro | Como o app identifica |
|---|---|
| **Tendência / estrutura** | Pivôs de topo e fundo → HH, HL, LH, LL |
| **BOS** | Fechamento além do último topo/fundo **a favor** da tendência |
| **CHoCH** | Fechamento além do último topo/fundo **contra** a tendência (possível reversão) |
| **Order Block (OB)** | Última vela contrária antes do movimento que gerou o BOS/CHoCH; status *ativo / testado / quebrado* |
| **Breaker Block** | OB que falhou (fechamento além da zona) e inverte de polaridade |
| **Fair Value Gap (FVG)** | Lacuna de 3 velas não preenchida (preenchimento parcial reduz a zona) |
| **Liquidez** | Topos/fundos iguais (EQH/EQL) ainda não varridos e *sweeps* (pavio rompe o nível e o candle fecha de volta) |
| **Suporte / Resistência** | Agrupamento de pivôs com 2+ toques |
| **Premium / Discount** | Posição do preço no range atual (compras em desconto, vendas em premium) |
| **Passos 1-3 do livro** | Tendência → OB de alta probabilidade → **entrada** na borda da zona, **stop** logo além dela, **alvos** nas estruturas/liquidez seguintes |

Cada ativo recebe um **score 0–100 de confluência** (estrutura 20, tempo maior 10, BOS/CHoCH 10, premium/discount 10,
zona 15, FVG 5, liquidez 10, risco/retorno 10, confirmações EMA200/volume/RSI/ADX 10), calculado para **compra** e para
**venda**; o lado mais forte vira o sinal. Para cada ativo o painel mostra: zona, entrada, stop, até 3 alvos com R:R,
estado (**EM ZONA** / aguardar / invalidado), regras de **saída e stop móvel**, calculadora de tamanho de posição e uma
leitura em texto. No gráfico aparecem BOS/CHoCH, HH/HL/LH/LL, OB, FVG, liquidez, S/R e as linhas do plano.

Há visão **diária** (com tendência semanal como "tempo maior") e **semanal**.

## Publicar no GitHub (uma vez só)

1. Crie um repositório no GitHub (público é o mais simples) e envie esta pasta inteira:
   ```bash
   git init && git add . && git commit -m "Smc_B3"
   git branch -M main
   git remote add origin https://github.com/SEU_USUARIO/Smc_B3.git
   git push -u origin main
   ```
2. No repositório: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. Aba **Actions** → workflow **Atualizar dados e publicar** → **Run workflow** (o primeiro push já dispara um).
   Em ~3–6 minutos o site fica em `https://SEU_USUARIO.github.io/Smc_B3/`.
4. A partir daí o GitHub roda sozinho **de hora em hora** (`.github/workflows/atualizar.yml`, cron `5 13-21 * * 1-5` em UTC).

As opções agora são um projeto separado (**Opcoes_B3**). Os dados são gerados a cada execução e publicados direto no Pages — **não são commitados**, então o repositório não cresce.

### No celular
Abra o endereço do site → menu do navegador → **Adicionar à tela inicial** (vira um app, em tela cheia).

### Limitações do agendamento do GitHub
- O horário do cron pode atrasar alguns minutos em horários de pico; não é tempo real.
- Em repositório público, o GitHub **desativa agendamentos após 60 dias sem atividade** — se isso acontecer, é só dar um push ou reativar na aba Actions.
- Cotações do Yahoo Finance podem ter atraso (~15 min). Se o Yahoo bloquear as IPs do GitHub em alguma execução, o workflow
  **falha de propósito** (menos de 50% dos ativos) e o site continua com a última versão boa.
- Para mudar a frequência/horário, edite a linha `cron` do workflow (horários em UTC = Brasília + 3h).

## Rodar no seu computador (Windows: um clique)

1. Instale o Python 3.10+ (python.org, marcando **Add Python to PATH**).
2. Dê dois cliques em **`1_testar_simulado.bat`** (sem internet) ou **`2_testar_dados_reais.bat`**. Ele instala as bibliotecas, gera os dados,
   inicia o servidor e **abre o navegador sozinho**. Deixe a janela preta aberta enquanto usa o site.
3. Depois, para só abrir o site de novo: **`3_abrir_site.bat`**. No celular (mesmo Wi-Fi) use o endereço "No celular" que a janela mostra.

Se a página não abrir: confirme que a janela do servidor continua aberta, use `http://localhost:8000` no próprio PC (o IP de rede só vale
para outros aparelhos) e, para o celular, libere a porta no firewall do Windows. Comandos manuais, se preferir:

```bash
pip install -r requirements.txt
python build_data.py --n 100 --out site        # gera site/data/*.json  (≈ 3–6 min)
python -m http.server 8000 --directory site    # abra http://localhost:8000
```
Para ver no celular na mesma rede Wi-Fi: `http://IP_DO_SEU_PC:8000`.
Sem internet, `python build_data.py --out site --demo` gera **dados simulados** (o site mostra um aviso).

## Arquivos

- `smc.py` — motor SMC (estrutura, BOS/CHoCH, OB, Breaker, FVG, liquidez, S/R, plano, score)
- `data.py` — universo (Fundamentus), candles (yfinance), modo demo
- `build_data.py` — gera os JSONs estáticos do scanner SMC
- `site/` — interface (HTML/JS, gráfico TradingView *Lightweight Charts* embutido, manifest PWA)
- `.github/workflows/atualizar.yml` — agendamento e deploy

## Ajustes rápidos
- Quantidade de ativos: `--n 100` no workflow e em `build_data.py`.
- Sensibilidade dos pivôs: `TF_PARAMS["1d"]["swing"]` em `smc.py` (menor = mais sinais/ruído).
- Folga do stop: `SL_BUFFER_ATR`; risco mínimo: `MIN_RISK_ATR`; tamanho mínimo de FVG: `MIN_FVG_ATR`.

## Aviso importante
Ferramenta educacional. O próprio material do livro admite que **não há evidência concreta** de que "smart money"
manipule o preço e que o SMC é, em boa parte, price action com outra terminologia. Os níveis são gerados por regras
automáticas, **não são recomendação de investimento**. Faça *backtest* e use stop. "Venda" pode significar sair de uma
posição comprada ou operar vendido (aluguel de ações/derivativos), o que tem riscos próprios.
