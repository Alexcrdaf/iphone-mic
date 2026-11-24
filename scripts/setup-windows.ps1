param(
    [int]$Port = 8443
)

Write-Host "=== Проверка настройки ПК для iPhone 14 Pro Микрофона ===" -ForegroundColor Cyan

# 1. Проверка VB-Cable
$vbDevices = Get-CimInstance Win32_SoundDevice | Where-Object { $_.Name -like "*VB-Audio*" }
if ($vbDevices) {
    Write-Host "[OK] Драйвер VB-Audio Virtual Cable найден:" -ForegroundColor Green
    $vbDevices | ForEach-Object { Write-Host "     - $($_.Name)" }
} else {
    Write-Host "[!] VB-Audio Virtual Cable НЕ установлен." -ForegroundColor Yellow
    $dlSetup = Join-Path $env:USERPROFILE "Downloads\VBCABLE_Driver_Pack46\VBCABLE_Setup_x64.exe"
    if (Test-Path $dlSetup) {
        Write-Host "    Найден распакованный установщик: $dlSetup"
        Write-Host "    Запустите его от имени администратора, нажмите Install Driver и перезагрузите ПК."
    } else {
        Write-Host "    Скачайте с https://vb-audio.com/Cable/ , распакуйте и запустите VBCABLE_Setup_x64.exe от имени администратора."
    }
}

# 2. Правило брандмауэра Windows для входящего порта 8443 (Profile Any, т.к. сеть может быть Public)
$ruleName = "iPhoneMicBridge_TCP_$Port"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[OK] Правило брандмауэра '$ruleName' уже существует." -ForegroundColor Green
} else {
    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        New-NetFirewallRule -DisplayName $ruleName `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort $Port `
            -RemoteAddress LocalSubnet `
            -Profile Any | Out-Null
        Write-Host "[OK] Добавлено правило брандмауэра для локальной подсети (порт $Port)." -ForegroundColor Green
    } else {
        Write-Host "[!] Чтобы добавить правило брандмауэра автоматически, запустите этот скрипт от имени администратора." -ForegroundColor Yellow
    }
}

# 3. Открыть окно устройств записи Windows, чтобы переименовать CABLE Output -> Микрофон iPhone 14 Pro
Write-Host ""
Write-Host "Открываю настройки устройств записи (Звук -> Запись)..." -ForegroundColor Cyan
Write-Host "Там найдите 'CABLE Output', нажмите 'Свойства' -> переименуйте в 'Микрофон iPhone 14 Pro' и выберите 'По умолчанию'."
Start-Process "control.exe" -ArgumentList "mmsys.cpl,,1"
