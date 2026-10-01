$ErrorActionPreference = 'Stop'

function Stop-WithMessage([string]$Message) {
    Write-Host "`n錯誤：$Message" -ForegroundColor Red
    exit 1
}

try {
    $repoRoot = $PSScriptRoot
    Set-Location -LiteralPath $repoRoot

    $insideRepo = git rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or $insideRepo -ne 'true') {
        Stop-WithMessage '此腳本必須放在 Git 專案資料夾內執行。'
    }

    $branch = (git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($branch)) {
        Stop-WithMessage '目前不是位於一般分支，請切換到要更新的分支後重試。'
    }

    $remoteUrl = git remote get-url origin 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remoteUrl)) {
        Stop-WithMessage '找不到 origin 遠端，請先設定 GitHub remote。'
    }

    # 將所有非 .gitignore 排除的新增、修改、刪除加入提交。
    # 注意：未忽略的草稿或資料也會一併提交，執行前可先檢查 git status。

    Write-Host "專案：$repoRoot" -ForegroundColor Cyan
    Write-Host "分支：$branch"
    Write-Host "遠端：$remoteUrl"

    git add -A
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'git add 失敗。' }



    $staged = git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Host "`n這些指定檔案沒有新的變更。先同步遠端……" -ForegroundColor Yellow
        git pull --rebase origin $branch
        if ($LASTEXITCODE -ne 0) {
            Stop-WithMessage '同步遠端失敗。請依 Git 顯示的訊息處理衝突，再重新執行。'
        }
        git push origin $branch
        if ($LASTEXITCODE -ne 0) { Stop-WithMessage '推送失敗，請檢查網路或 GitHub 登入狀態。' }
        Write-Host '`n已確認 GitHub 與本機分支同步。' -ForegroundColor Green
        exit 0
    }
    if ($LASTEXITCODE -ne 1) { Stop-WithMessage '無法檢查暫存變更。' }

    Write-Host "`n即將提交以下檔案：" -ForegroundColor Cyan
    git diff --cached --stat
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage '讀取變更摘要失敗。' }

    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
    $message = "更新當沖技術策略設定 ($stamp)"
    git commit -m $message
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage '建立 commit 失敗；請檢查 Git 使用者名稱、電子郵件或提交訊息。' }

    Write-Host "`n先同步 GitHub 上的最新提交……" -ForegroundColor Cyan
    git pull --rebase origin $branch
    if ($LASTEXITCODE -ne 0) {
        Stop-WithMessage '遠端同步遇到衝突。請先在終端機依 Git 指示解決衝突、完成 rebase，再重新執行腳本。'
    }

    Write-Host "`n正在推送到 GitHub……" -ForegroundColor Cyan
    git push origin $branch
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage '推送失敗，請檢查網路或 GitHub 登入狀態後重試。' }

    Write-Host "`n完成！已更新 GitHub 分支 $branch。" -ForegroundColor Green
    exit 0
}
catch {
    Stop-WithMessage $_.Exception.Message
}


