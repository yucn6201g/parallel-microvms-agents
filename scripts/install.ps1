# Windows 用のセットアップ: 前提ツールの確認、config.env の作成、bin フォルダのユーザー PATH への追加。
# 何度実行しても同じ結果になる。
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoPath                 # PATH を書き換えない
#   (古い mvm エイリアスの行を消す先は -ProfilePath <ファイル> で指定できる)
#
# 日本語版 Windows PowerShell 5.1 でも読めるよう、このファイルは UTF-8 (BOM 付き) で保存している。
param(
    [string]$ProfilePath = $PROFILE.CurrentUserAllHosts,  # 古いエイリアスを消す先
    [switch]$NoPath
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

# bin フォルダをユーザーの PATH に足す (HKCUEnvironment)。PowerShell 5.1 / 7 (pwsh) / cmd.exe のどれからでも mvm で呼べる
if (-not $NoPath) {
    $bin = Join-Path $Root 'bin'
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') {
        Ng "PATH への登録は Windows 用です。bin フォルダ ($bin) を PATH に足してください"
    } else {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        # 展開前の値のまま読み書きして、%USERPROFILE% などを展開済みの文字列に変えない
        $cur = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $entries = @($cur -split ';' | Where-Object { $_ })
        if ($entries | Where-Object { $_.TrimEnd('\') -ieq $bin.TrimEnd('\') }) {
            Ok "bin フォルダはすでにユーザーの PATH にあります: $bin"
        } else {
            $key.SetValue('Path', (($entries + $bin) -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
            # 変数を 1 つ消す操作で、エクスプローラーなどに環境変数の変更を通知する (新しい端末にすぐ反映される)
            [Environment]::SetEnvironmentVariable('PMVA_PATH_NOTIFY', $null, 'User')
            Ok "bin フォルダをユーザーの PATH に追加しました: $bin"
        }
        $key.Close()
        Write-Output '     いま開いている PowerShell / コマンドプロンプト / VS Code の端末は、閉じて開き直すと mvm が使えます'
    }
}

# 以前のバージョンが書いた mvm エイリアスの行を消す (PATH の mvm.cmd と二重になり、フォルダを動かすと壊れるため)
$ProfilePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ProfilePath)
if (Test-Path -LiteralPath $ProfilePath) {
    $old = @(Get-Content -LiteralPath $ProfilePath -Encoding UTF8)
    $kept = @($old | Where-Object { $_ -notmatch ' # pmva$' })
    if ($kept.Count -ne $old.Count) {
        [System.IO.File]::WriteAllLines($ProfilePath, [string[]]$kept, (New-Object System.Text.UTF8Encoding $true))
        Ok "古い mvm エイリアスの行を消しました: $ProfilePath"
    }
}

Write-Output ''
if ($script:Missing -gt 0) {
    Write-Output "足りないツールが $($script:Missing) 個あります。入れてから mvm doctor で確認してください"
} else {
    Write-Output '次は: 管理者のプロファイルを用意して mvm setup --profile admin (docs\quickstart.md の手順 4)'
}
