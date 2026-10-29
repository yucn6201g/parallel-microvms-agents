# Windows 用のセットアップ: 前提ツールの確認、config.env の作成、mvm エイリアスの登録。
# 何度実行しても同じ結果になる (エイリアスの行は書き換える)。
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -ProfilePath <ファイル>   # 登録先を指定
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoAlias                  # 登録しない
#
# 日本語版 Windows PowerShell 5.1 でも読めるよう、このファイルは UTF-8 (BOM 付き) で保存している。
param(
    [string]$ProfilePath = $PROFILE.CurrentUserAllHosts,
    [switch]$NoAlias
)
# 'Stop' にすると、Windows PowerShell 5.1 では外部コマンドの stderr 出力 (py が見つからない等) で止まってしまう
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$script:Missing = 0
function Ok($msg) { Write-Output "[OK] $msg" }
function Ng($msg) { Write-Output "[NG] $msg"; $script:Missing++ }

function Find-Python {
    # mvm.cmd と同じ順で探す (py ランチャー → python)
    foreach ($cand in @(@('py', '-3'), @('python'))) {
        if (-not (Get-Command $cand[0] -ErrorAction SilentlyContinue)) { continue }
        $exe = $cand[0]
        $rest = @($cand | Select-Object -Skip 1)
        & $exe @rest -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>$null
        if ($LASTEXITCODE -eq 0) { return (& $exe @rest --version 2>&1 | Out-String).Trim() }
    }
    return $null
}

$py = Find-Python
if ($py) { Ok $py } else { Ng 'Python 3.9 以上が見つかりません (winget install Python.Python.3.13 / https://www.python.org/downloads/)' }

if (Get-Command aws -ErrorAction SilentlyContinue) {
    $v = (& aws --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { Ng "aws --version が失敗しました: $v" } else { Ok "$v (lambda-microvms に対応しているかは mvm doctor で確認します)" }
} else {
    Ng 'AWS CLI v2 が見つかりません (winget install Amazon.AWSCLI)'
}

if (Get-Command ssh -ErrorAction SilentlyContinue) {
    Ok 'OpenSSH クライアント (ssh)'
} else {
    Ng 'ssh が見つかりません (設定 → システム → オプション機能 → OpenSSH クライアント)'
}

# mvm と同じく PMVA_CONFIG で置き場所を変えられる
$config = if ($env:PMVA_CONFIG) { $env:PMVA_CONFIG } else { Join-Path $Root 'config.env' }
if (Test-Path $config) {
    Ok "config.env はすでにあります (上書きしません): $config"
} else {
    Copy-Item (Join-Path $Root 'config.example.env') $config -ErrorAction Stop
    Ok "config.env を作成しました。AWS_REGION などを確認してください: $config"
}

if (-not $NoAlias) {
    $ProfilePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ProfilePath)
    $line = "Set-Alias mvm '" + (Join-Path $Root 'bin\mvm.cmd') + "' # pmva"
    $dir = Split-Path -Parent $ProfilePath
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $lines = @()
    if (Test-Path $ProfilePath) {
        $lines = @(Get-Content -LiteralPath $ProfilePath -Encoding UTF8 | Where-Object { $_ -notmatch ' # pmva$' })
    }
    $lines += $line
    # BOM 付き UTF-8 で保存 (Windows PowerShell 5.1 は BOM なしを ANSI として読むため)
    [System.IO.File]::WriteAllLines($ProfilePath, [string[]]$lines, (New-Object System.Text.UTF8Encoding $true))
    Ok "エイリアスを登録しました: $ProfilePath (新しい PowerShell を開くと有効になります)"
    $policy = Get-ExecutionPolicy -Scope CurrentUser
    if ($policy -eq 'Restricted' -or ($policy -eq 'Undefined' -and (Get-ExecutionPolicy) -eq 'Restricted')) {
        Write-Output '     プロファイルを読み込むには次を 1 回実行してください: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned'
    }
}

Write-Output ''
if ($script:Missing -gt 0) {
    Write-Output "足りないツールが $($script:Missing) 個あります。入れてから mvm doctor で確認してください"
} else {
    Write-Output '次は: mvm doctor → mvm infra deploy (docs\setup.md)'
}
