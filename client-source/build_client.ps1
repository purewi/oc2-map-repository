$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到虚拟环境 Python：$python"
}
Push-Location $projectRoot
try {
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "测试失败" }
    $clientVersion = (& $python -c "from client import __version__; print(__version__)").Trim()
    if (-not $clientVersion) { throw "无法读取客户端版本号" }
    $previousBuildVariant = $env:OC2_CLIENT_BUILD_VARIANT
    try {
        $env:OC2_CLIENT_BUILD_VARIANT = "cos"
        & $python -m PyInstaller --noconfirm --clean .\OC2MapManager.spec
        if ($LASTEXITCODE -ne 0) { throw "COS 专享版客户端打包失败" }
        $env:OC2_CLIENT_BUILD_VARIANT = "public"
        & $python -m PyInstaller --noconfirm --clean .\OC2MapManager.spec
        if ($LASTEXITCODE -ne 0) { throw "公开版客户端打包失败" }
    }
    finally {
        $env:OC2_CLIENT_BUILD_VARIANT = $previousBuildVariant
    }
    $clients = @(
        @{ Source = (Join-Path $projectRoot "dist\OC2MapManager-COS-VIP.exe"); Release = "OC2MapManager-COS-VIP-v$clientVersion-win64.exe" },
        @{ Source = (Join-Path $projectRoot "dist\OC2MapManager-Public.exe"); Release = "OC2MapManager-Public-v$clientVersion-win64.exe" }
    )
    $previousLocalAppData = $env:LOCALAPPDATA
    try {
        foreach ($client in $clients) {
            $env:LOCALAPPDATA = Join-Path $projectRoot ("temp\client-build-smoke-" + $client.Release)
            $process = Start-Process -FilePath $client.Source -ArgumentList "--smoke-test" -WindowStyle Hidden -PassThru
            if (-not $process.WaitForExit(30000)) {
                $process | Stop-Process -Force
                throw "客户端冒烟测试超时：$($client.Release)"
            }
            if ($process.ExitCode -ne 0) {
                throw "客户端冒烟测试失败：$($client.Release)，退出码 $($process.ExitCode)"
            }
        }
    }
    finally {
        $env:LOCALAPPDATA = $previousLocalAppData
    }
    $releaseDirectory = Join-Path $projectRoot "release"
    New-Item -ItemType Directory -Path $releaseDirectory -Force | Out-Null
    foreach ($client in $clients) {
        $releaseExe = Join-Path $releaseDirectory $client.Release
        Copy-Item -LiteralPath $client.Source -Destination $releaseExe -Force
        Write-Host "客户端单文件已生成：$releaseExe"
    }
}
finally {
    Pop-Location
}
