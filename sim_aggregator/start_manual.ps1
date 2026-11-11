param([string]$LogFile="C:\Users\MAMA TECH SOLUTIONS\sim_aggregator\server.log")

# main.py imports its siblings with package-relative imports (from . import ...),
# so uvicorn has to load it as "sim_aggregator.main" from the PARENT folder.
# Pointing uvicorn at the folder itself fails with "attempted relative import".
$root = "C:\Users\MAMA TECH SOLUTIONS"
$python = "C:\Users\MAMA TECH SOLUTIONS\sim_aggregator\.venv\Scripts\python.exe"

Set-Location $root
& $python -m uvicorn sim_aggregator.main:app --host 127.0.0.1 --port 8000 *>&1 | Out-File -FilePath $LogFile -Append
Write-Host "Server log written to $LogFile"
