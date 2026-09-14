@echo off
setlocal
pushd "%~dp0\.."

echo == reindex: recreating container so .env changes take effect ==
docker compose -f compose.yml up -d --force-recreate wellisearch
if errorlevel 1 goto fail

echo == waiting for container readiness (30 checks, 2s apart) ==
set TRY=0

:probe
if %TRY% geq 30 goto timeout
docker compose -f compose.yml exec wellisearch python -m wellisearch.reindex --dry-run >nul 2>&1
if not errorlevel 1 goto ready
timeout /t 2 /nobreak >nul
set /a TRY+=1
goto probe

:ready
echo Container is ready (dry-run probe passed).
echo == re-chunking and re-embedding every page; this can take a while ==
docker compose -f compose.yml exec wellisearch python -m wellisearch.reindex --force
if errorlevel 1 goto fail
popd
exit /b 0

:timeout
echo.
echo wellisearch did not become ready in time. 1>&2
echo Check the container logs with: docker compose -f compose.yml logs wellisearch 1>&2
popd
exit /b 1

:fail
echo.
echo reindex failed, see error above. 1>&2
popd
exit /b 1
