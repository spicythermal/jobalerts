# Windows: start jobalerts at logon with Task Scheduler. Run once in PowerShell from the jobalerts folder:
#
#   powershell -ExecutionPolicy Bypass -File contrib\jobalerts-startup.ps1
#
# It registers a task "jobalerts" that runs `jobalerts run` hidden when you log in and restarts it if it
# stops. The log goes to data\jobalerts.log (the first-run setup code for the web UI is printed there).
# Remove it with:  Unregister-ScheduledTask -TaskName jobalerts -Confirm:$false
# After sleep or shutdown it catches up on start (docs/desktop-mode.md).

$folder = (Resolve-Path "$PSScriptRoot\..").Path
$exe = Join-Path $folder ".venv\Scripts\jobalerts.exe"
if (-not (Test-Path $exe)) { throw "Install jobalerts first: python -m venv .venv; .venv\Scripts\pip install -e ." }
New-Item -ItemType Directory -Force -Path (Join-Path $folder "data") | Out-Null
$log = Join-Path $folder "data\jobalerts.log"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$exe`" run >> `"$log`" 2>&1`"" -WorkingDirectory $folder
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "jobalerts" -Action $action -Trigger $trigger -Settings $settings `
    -Description "jobalerts: job board watcher and web UI" -Force | Out-Null
Start-ScheduledTask -TaskName "jobalerts"
Write-Host "jobalerts starts at logon now (and is running). Log: $log"
