@echo off
rem Mutes the ads in the tennis stream playing in Chrome (port 9224). Close this window to stop.
rem Extra arguments pass through, e.g.  mute_ads.bat --tab tennis
python "%~dp0mute_ads.py" %*
if errorlevel 1 pause
