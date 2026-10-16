@echo off
chcp 65001 >nul
title Foto de mercado - Argenprop
cd /d "%~dp0"

echo ==========================================================
echo   FOTO DE MERCADO - Argenprop
echo   Campana / Zarate / Exaltacion de la Cruz - Casas y Deptos
echo ==========================================================
echo.
echo  Se va a abrir una ventana de Chrome: dejala abierta (podes minimizarla).
echo  Si Argenprop pide un CAPTCHA, resolvelo en esa ventana y sigue solo.
echo  No cierres esta ventana ni suspendas la compu hasta que termine.
echo  Si se corta, volve a hacer doble click: sigue desde donde quedo.
echo.

py -3.12 scraper_mercado.py

echo.
if %ERRORLEVEL%==0 (
  echo  LISTO. La foto ya esta en la hoja "Base" del Google Sheet.
) else (
  echo  Hubo un error. Saca una captura de esta ventana y pasasela a Claude.
)
echo.
pause
