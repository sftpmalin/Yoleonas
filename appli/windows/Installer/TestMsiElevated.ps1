$ErrorActionPreference = 'Stop'

$installerRoot = Split-Path -Parent $PSScriptRoot
$msi = Join-Path $installerRoot 'dist\YoleoAgent.msi'
$installedExe = 'C:\Program Files\Sftpmalin\Yoleo Agent\YoleoAgent.exe'
$shortcut = 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Yoleo Agent\Yoleo Agent.lnk'
$runKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'
$resultFile = Join-Path $PSScriptRoot 'elevated-test-result.txt'
$installLog = Join-Path $PSScriptRoot 'elevated-install.log'
$uninstallLog = Join-Path $PSScriptRoot 'elevated-uninstall.log'
$results = [System.Collections.Generic.List[string]]::new()
$installed = $false

try {
    $install = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" `
        -ArgumentList @('/i', $msi, '/qn', '/norestart', '/L*v', $installLog) `
        -WindowStyle Hidden -Wait -PassThru
    $results.Add("MSI_INSTALL_EXIT=$($install.ExitCode)")
    if ($install.ExitCode -ne 0) {
        throw "Installation MSI echouee avec le code $($install.ExitCode)."
    }
    $installed = $true

    $exePresent = Test-Path -LiteralPath $installedExe
    $shortcutPresent = Test-Path -LiteralPath $shortcut
    $autoStart = Get-ItemPropertyValue -Path $runKey -Name 'YoleoAgent' -ErrorAction SilentlyContinue
    $autoStartOk = $autoStart -match 'Program Files\\Sftpmalin\\Yoleo Agent\\YoleoAgent\.exe'
    $results.Add("EXE_IN_PROGRAM_FILES=$exePresent")
    $results.Add("START_MENU_SHORTCUT=$shortcutPresent")
    $results.Add("AUTOSTART_VALUE=$autoStart")
    $results.Add("AUTOSTART_OK=$autoStartOk")
    if (-not $exePresent -or -not $shortcutPresent -or -not $autoStartOk) {
        throw 'Verification des fichiers ou du demarrage automatique echouee.'
    }

    $selfTest = Start-Process -FilePath $installedExe -ArgumentList '--self-test' `
        -WindowStyle Hidden -Wait -PassThru
    $results.Add("INSTALLED_EXE_SELFTEST_EXIT=$($selfTest.ExitCode)")
    if ($selfTest.ExitCode -ne 0) {
        throw 'Autotest de l EXE installe echoue.'
    }
}
catch {
    $results.Add("ERROR=$($_.Exception.Message)")
}
finally {
    if ($installed) {
        $uninstall = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" `
            -ArgumentList @('/x', $msi, '/qn', '/norestart', '/L*v', $uninstallLog) `
            -WindowStyle Hidden -Wait -PassThru
        $results.Add("MSI_UNINSTALL_EXIT=$($uninstall.ExitCode)")
    }

    $results.Add("EXE_REMOVED=$(-not (Test-Path -LiteralPath $installedExe))")
    $results.Add("SHORTCUT_REMOVED=$(-not (Test-Path -LiteralPath $shortcut))")
    $remainingAutoStart = Get-ItemPropertyValue -Path $runKey -Name 'YoleoAgent' -ErrorAction SilentlyContinue
    $results.Add("AUTOSTART_REMOVED=$([string]::IsNullOrEmpty($remainingAutoStart))")
    $results | Set-Content -LiteralPath $resultFile -Encoding UTF8
}

if ($results.Where({ $_ -like 'ERROR=*' }).Count -gt 0) {
    exit 1
}
exit 0
