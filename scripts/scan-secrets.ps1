# 提交前扫描：密钥、令牌、真实 QQ 号、聊天数据
# 用法：pwsh -File scripts/scan-secrets.ps1 [-Path <目录>]

param(
    [string]$Path = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$findings = New-Object System.Collections.Generic.List[string]

# 1) 不该进仓库的路径
$forbiddenDirs = @('data', 'logs', 'temp')
foreach ($dir in $forbiddenDirs) {
    $full = Join-Path $Path $dir
    if (Test-Path $full) { $findings.Add("存在不该提交的目录: $dir/") }
}
foreach ($name in @('webui.json', 'model_catalog.toml')) {
    $hit = Get-ChildItem -Path $Path -Recurse -File -Filter $name -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\host\\' }
    foreach ($h in $hit) { $findings.Add("存在含机密的本机文件: $($h.FullName.Substring($Path.Length).TrimStart('\'))") }
}

# 2) 文本内容里的敏感串
$patterns = [ordered]@{
    'API Key (sk-)'        = 'sk-[A-Za-z0-9\-_]{16,}'
    '通用 api_key 赋值'    = 'api_key\s*=\s*"[^"\s]{12,}"'
    '通用 token 赋值'      = '(?i)(access_token|auth_token|password)\s*[=:]\s*"[^"\s]{12,}"'
    'QQ 号（10 位数字）'   = '\b[1-9]\d{9}\b'
}

$files = Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\\.git\\' -and $_.Extension -in @('.toml', '.json', '.md', '.ps1', '.yml', '.yaml', '.txt', '.env', '') }

foreach ($file in $files) {
    $text = Get-Content -Path $file.FullName -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $text) { continue }
    foreach ($label in $patterns.Keys) {
        foreach ($m in [regex]::Matches($text, $patterns[$label])) {
            # 占位符放行
            if ($m.Value -match 'YOUR_|PLACEHOLDER|example|example\.com') { continue }
            $line = ($text.Substring(0, $m.Index) -split "`n").Count
            $findings.Add("$label -> $($file.FullName.Substring($Path.Length).TrimStart('\')):$line")
        }
    }
}

if ($findings.Count -eq 0) {
    Write-Host "OK: 未发现密钥、真实 QQ 号或聊天数据目录。" -ForegroundColor Green
    exit 0
}

Write-Host "发现 $($findings.Count) 处需要处理：" -ForegroundColor Yellow
$findings | Sort-Object -Unique | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
exit 1
