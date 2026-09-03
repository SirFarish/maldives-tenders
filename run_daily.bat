@echo off
REM Maldives Tender Collector - daily run
REM Generates today's combined tender/gazette dashboard into .\output\latest.html
cd /d "%~dp0"
"C:\Users\ITD\AppData\Local\Programs\Python\Python312\python.exe" "%~dp0tender_collector.py" >> "%~dp0output\run.log" 2>&1
