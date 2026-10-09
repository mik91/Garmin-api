param([switch]$Legacy)

Set-Location $PSScriptRoot

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . ".\.venv\Scripts\Activate.ps1"
}

if ($Legacy) {
    python -m streamlit run app.py
} else {
    python -m garmin_ai.web_server
}
