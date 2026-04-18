@echo off
REM ============================================================
REM Скачивание всех пакетов ShowTail для офлайн-установки (Windows)
REM ============================================================
REM
REM Использование:
REM   1. На машине с интернетом:
REM      scripts\download_packages.bat
REM
REM   2. Перенести папку offline_packages\ на офлайн-машину
REM
REM   3. На офлайн-машине:
REM      pip install --no-index --find-links=offline_packages\ -r requirements.txt
REM      pip install --no-index --find-links=offline_packages\ -r requirements-dev.txt
REM
REM ============================================================

set PACKAGES_DIR=offline_packages

echo ========================================
echo  ShowTail — Скачивание пакетов для офлайн
echo  Папка: %PACKAGES_DIR%\
echo ========================================

if exist "%PACKAGES_DIR%" rmdir /S /Q "%PACKAGES_DIR%"
mkdir "%PACKAGES_DIR%"

echo.
echo [1/3] Скачивание production-зависимостей...
pip download -r requirements.txt -d %PACKAGES_DIR% --no-cache-dir
if errorlevel 1 goto :error

echo.
echo [2/3] Скачивание dev-зависимостей...
pip download -r requirements-dev.txt -d %PACKAGES_DIR% --no-cache-dir
if errorlevel 1 goto :error

echo.
echo [3/3] Docker-образы (скачать вручную):
echo.
echo   docker pull postgres:16
echo   docker pull rabbitmq:3-management
echo   docker pull redis:7-alpine
echo   docker pull minio/minio
echo   docker pull axllent/mailpit
echo.
echo   docker save postgres:16 rabbitmq:3-management redis:7-alpine minio/minio axllent/mailpit -o %PACKAGES_DIR%\docker_images.tar
echo.
echo   На офлайн-машине:
echo   docker load -i %PACKAGES_DIR%\docker_images.tar

echo.
echo ========================================
echo  Готово!
echo  Папка: %PACKAGES_DIR%\
echo ========================================
echo.
echo  Для установки на офлайн-машине:
echo    pip install --no-index --find-links=%PACKAGES_DIR%\ -r requirements.txt
echo    pip install --no-index --find-links=%PACKAGES_DIR%\ -r requirements-dev.txt
echo ========================================
goto :end

:error
echo.
echo ОШИБКА при скачивании пакетов!
exit /b 1

:end
