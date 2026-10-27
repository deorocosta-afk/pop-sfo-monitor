# Local AA direct check (residential browser companion)
#
# Runs after the GitHub morning scan, opens aa.com in a normal Chrome profile,
# checks the top date pairs directly, sends a Telegram message when a price is
# found, and commits only the text report (the runtime DB is not pushed, so it
# cannot conflict with the GitHub workflow's binary DB commits).
#
# Registered as a Windows Scheduled Task: "POP-SFO Local AA Direct Check".

$ErrorActionPreference = 'Continue'
$root = 'C:\Herness\pop-sfo-monitor'
$python = 'C:\Users\N\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
$log = Join-Path $root 'local_aa_check.log'
$report = Join-Path $root 'direct_aa_report.md'
$chromeProfile = Join-Path $root '.chrome_aa_profile'
$dashboard = 'https://deorocosta-afk.github.io/pop-sfo-monitor/pop_sfo_dashboard.html'
$repo = 'https://github.com/deorocosta-afk/pop-sfo-monitor.git'

function Log([string]$message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
    Write-Host $line
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

Set-Location $root
Log "=== local AA direct check start ==="

if (-not (Test-Path $python)) {
    Log "Python runtime not found: $python"
    exit 1
}

# Clean tracked runtime files so a rebase never hits a dirty binary DB.
git checkout -- pop_sfo_prices.db direct_airline_report.md direct_aa_report.md 2>$null | Out-Null
git pull --rebase origin main 2>&1 | ForEach-Object { Log $_ }

$env:DIRECT_CHROME_PROFILE = $chromeProfile
New-Item -ItemType Directory -Path $chromeProfile -Force | Out-Null

& $python direct_airlines.py --top 5 --airlines AA 2>&1 | ForEach-Object { Log $_ }

if (-not (Test-Path (Join-Path $root 'direct_airline_report.md'))) {
    Log "No report generated"
    exit 1
}

Copy-Item -LiteralPath (Join-Path $root 'direct_airline_report.md') -Destination $report -Force

# Keep the shared report and DB untouched; only direct_aa_report.md is committed.
git checkout -- pop_sfo_prices.db direct_airline_report.md 2>$null | Out-Null

$okRows = Select-String -Path $report -Pattern '^\| AA \|' | Where-Object { $_.Line -match '\| OK \|' }
if ($okRows) {
    $lines = @()
    foreach ($row in $okRows) {
        $parts = $row.Line.Trim('|').Split('|') | ForEach-Object { $_.Trim() }
        if ($parts.Count -ge 7) {
            $lines += "$($parts[1]) → $($parts[2]): <b>$($parts[4])</b> ($($parts[5]), $($parts[6]))"
        }
    }
    if ($lines.Count -gt 0) {
        $token = [Environment]::GetEnvironmentVariable('TELEGRAM_BOT_TOKEN', 'User')
        $chat = [Environment]::GetEnvironmentVariable('TELEGRAM_CHAT_ID', 'User')
        if ($token -and $chat) {
            $text = "🇺🇸 <b>AA direct check (локальный браузер)</b>`n`n" +
                    ($lines -join "`n") + "`n`n" +
                    "<a href=`"$dashboard`">Открыть дашборд</a>"
            try {
                $resp = Invoke-RestMethod -Method Post `
                    -Uri "https://api.telegram.org/bot$token/sendMessage" `
                    -ContentType 'application/json' `
                    -Body (@{ chat_id = $chat; text = $text; parse_mode = 'HTML'; disable_web_page_preview = $true } | ConvertTo-Json)
                Log "Telegram sent: $($resp.ok)"
            } catch {
                Log "Telegram error: $($_.Exception.Message)"
            }
        } else {
            Log "Telegram token/chat missing in User environment"
        }
    }
} else {
    Log "No AA OK rows (Akamai challenge or no flights)"
}

git add direct_aa_report.md
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git -c user.name='deorocosta-afk' -c user.email='admin@cabrents.com' commit -m "Local AA direct check $(Get-Date -Format 'yyyy-MM-dd')" 2>&1 | ForEach-Object { Log $_ }
    $env:GIT_TERMINAL_PROMPT = '0'
    for ($i = 1; $i -le 3; $i++) {
        git pull --rebase -X theirs origin main 2>&1 | ForEach-Object { Log $_ }
        git push origin main 2>&1 | ForEach-Object { Log $_ }
        if ($LASTEXITCODE -eq 0) { Log "Push succeeded on attempt $i"; break }
        git rebase --abort 2>$null | Out-Null
        Start-Sleep -Seconds (5 * $i)
    }
} else {
    Log "No report changes to commit"
}

Log "=== local AA direct check done ==="
