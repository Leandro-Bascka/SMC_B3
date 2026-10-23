@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py
%PY% --version >nul 2>nul || (
  echo [ERRO] Python nao encontrado. Instale em python.org marcando "Add Python to PATH" e rode de novo.
  pause
  exit /b 1
)
echo === Instalando bibliotecas (so demora na primeira vez) ===
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERRO] Falha ao instalar as bibliotecas. Verifique a internet e tente de novo.
  pause
  exit /b 1
)
echo.
echo === Coletando dados REAIS (scanner SMC: ~3 a 6 min) ===
%PY% build_data.py --n 100 --out site --min-ratio 0.05
if not exist site\data\meta.json (
  echo.
  echo [ERRO] Os dados NAO foram gerados - leia as mensagens acima para ver o motivo.
  echo        Para ver o app funcionando mesmo assim, rode 1_testar_simulado.bat
  pause
  exit /b 1
)
echo.
%PY% servir.py
pause
