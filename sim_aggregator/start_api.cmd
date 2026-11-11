@echo off
rem main.py uses package-relative imports, so run uvicorn from the PARENT folder
rem and import sim_aggregator.main. Running from inside the package fails.
set PYTHON=C:\Users\MAMA TECH SOLUTIONS\sim_aggregator\.venv\Scripts\python.exe
cd /d C:\Users\MAMA TECH SOLUTIONS
%PYTHON% -m uvicorn sim_aggregator.main:app --host 127.0.0.1 --port 8000 > sim_aggregator\server.log 2>&1
