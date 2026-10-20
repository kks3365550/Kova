# =========================================================================
# 백업시스템 (BackupSystem) 자체 서명 코드사인(Code Signing) 인증서 발급기
# =========================================================================
# 용도: 사내 및 개발 배포용 자체 서명 인증서를 생성하고 로컬 신뢰 루트에 등록하여
#       Inno Setup 인스톨러 및 실행 바이너리의 윈도우 SmartScreen 신뢰성 확보
# =========================================================================

[CmdletBinding()]
param(
    [string]$CertName = "BackupSystem Code Signing CA",
    [string]$CertPath = "$PSScriptRoot\..\certs\BackupSystem_CodeSign.pfx",
    [string]$Password = ""
)

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  백업시스템 자체 서명 코드사인 인증서 생성 및 등록 도구" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. 대상 디렉터리 준비
$certDir = Split-Path $CertPath -Parent
if (-not (Test-Path $certDir)) {
    New-Item -ItemType Directory -Path $certDir -Force | Out-Null
}

# 2. 코드사인 자체 인증서 생성
Write-Host "[1/3] 자체 서명 코드사인 인증서 발급 중 ($CertName)..." -ForegroundColor Yellow
$cert = New-SelfSignedCertificate -Type CodeSigningCert `
    -Subject "CN=$CertName, O=Samyoung Delicafresh, C=KR" `
    -KeyUsage DigitalSignature `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -NotAfter (Get-Date).AddYears(5)

Write-Host "      - 지문(Thumbprint): $($cert.Thumbprint)" -ForegroundColor Green

# 3. PFX 파일로 내보내기
Write-Host "[2/3] 인증서 파일(.pfx) 내보내기 중..." -ForegroundColor Yellow
if (-not $Password) {
    $Password = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 16 | ForEach-Object {[char]$_})
    Write-Host "      - 자동 생성된 PFX 보호 비밀번호: $Password" -ForegroundColor Yellow
}
$securePassword = ConvertTo-SecureString -String $Password -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath $CertPath -Password $securePassword | Out-Null
Write-Host "      - 저장 위치: $CertPath" -ForegroundColor Green

# 4. 로컬 사용자의 신뢰할 수 있는 루트 인증 기관(Trusted Root)에 인증서 등록 (경고창 차단)
Write-Host "[3/3] '신뢰할 수 있는 루트 인증 기관' 저장소에 등록 중..." -ForegroundColor Yellow
$rootStore = New-Object System.Security.Cryptography.X509Certificates.X509Store(
    [System.Security.Cryptography.X509Certificates.StoreName]::Root,
    [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
)
$rootStore.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
$rootStore.Add($cert)
$rootStore.Close()
Write-Host "      - 신뢰할 수 있는 루트 인증 기관 등록 완료!" -ForegroundColor Green

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  성공: 코드사인 인증서 발급 및 시스템 신뢰 등록이 완료되었습니다!" -ForegroundColor Green
Write-Host "  Inno Setup 컴파일 시 위 PFX 파일로 디지털 서명이 가능합니다." -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
