@echo off
REM Start memory-kernel.
REM Why the env cleanup: httpx fails to parse "[::1]" inside NO_PROXY
REM (InvalidURL: Invalid port ':1]'), which silently downgrades the OpenAI
REM extractor to mock and skips embedding writes. DashScope and the local
REM kernel are both reachable directly, so proxies are cleared here.
setlocal
cd /d "%~dp0"
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set http_proxy=
set https_proxy=
set all_proxy=
set NO_PROXY=localhost,127.0.0.1,::1
set no_proxy=localhost,127.0.0.1,::1
start "memory-kernel" /b ".venv\Scripts\memory-kernel.exe" serve > kernel.out.log 2> kernel.err.log
endlocal
