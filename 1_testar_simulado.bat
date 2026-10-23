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
echo === Gerando dados SIMULADOS (sem internet) ===
%PY% build_data.py --n 30 --out site --demo
if not exist site\data\meta.json (
  echo.
  echo [ERRO] Os dados NAO foram gerados - leia as mensagens acima para ver o motivo.
  echo        Para ver o app funcionando mesmo assim, rode de novo
  pause
  exit /b 1
)
echo.
%PY% servir.py
pause
