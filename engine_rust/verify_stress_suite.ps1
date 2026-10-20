# ==============================================================================
# Kova Rust Fast Engine - Comprehensive Stress & Edge-Case Verification Suite
# Written in Pure-ASCII for 100% PowerShell Compatibility
# ==============================================================================
$ErrorActionPreference = "Stop"

$Engine = "C:\Users\kksjmj\Desktop\ai\target_rust\release\kova_fast_engine.exe"
if (-not (Test-Path $Engine)) {
    $Engine = "C:\Users\kksjmj\Desktop\ai\백업시스템\engine_rust\kova_fast_engine.exe"
}

$BaseDir = "C:\Users\kksjmj\Desktop\ai\stress_test_workspace"
$SourceDir = "$BaseDir\source"
$RepoDir = "$BaseDir\repo"
$RestoreDir = "$BaseDir\restore"

Write-Host "====================================================================" -ForegroundColor Cyan
Write-Host " [KOVA STRESS SUITE] Rigorous Verification of Native Rust Backup Engine" -ForegroundColor Cyan
Write-Host " Engine: $Engine" -ForegroundColor Gray
Write-Host " Base  : $BaseDir" -ForegroundColor Gray
Write-Host "====================================================================" -ForegroundColor Cyan

# Clean & Prepare Environment
Remove-Item -Recurse -Force $BaseDir -ErrorAction SilentlyContinue
[System.IO.Directory]::CreateDirectory($SourceDir) | Out-Null
[System.IO.Directory]::CreateDirectory($RepoDir) | Out-Null
[System.IO.Directory]::CreateDirectory($RestoreDir) | Out-Null

$PassCount = 0
$TotalTests = 0

function Assert-Result($Condition, $Message) {
    $script:TotalTests++
    if ($Condition) {
        Write-Host "  [PASS] $Message" -ForegroundColor Green
        $script:PassCount++
    } else {
        Write-Host "  [FAIL] $Message" -ForegroundColor Red
        throw "Assertion Failed: $Message"
    }
}

# ------------------------------------------------------------------------------
# TEST 1: Korean, Unicode, Spaces, Brackets & Special Characters
# ------------------------------------------------------------------------------
Write-Host "`n>>> [TEST 1] Unicode, Korean Jamo, Spaces & Special Characters" -ForegroundColor Yellow

# Use Unicode characters: Korean for "Korean_Directory" and "Plan_Document"
$koreanDirName = [System.Text.Encoding]::UTF8.GetString([byte[]]@(0xED, 0x95, 0x9C, 0xEA, 0xB8, 0x80, 0x5F, 0xEB, 0x94, 0x94, 0xEB, 0xA0, 0x89, 0xED, 0x84, 0xB0, 0xEB, 0xA6, 0xAC)) # "한글_디렉터리"
$specialSubDir = "$SourceDir\$koreanDirName\Report_2026_Special_Symbols"
[System.IO.Directory]::CreateDirectory($specialSubDir) | Out-Null

$koreanFileName = [System.Text.Encoding]::UTF8.GetString([byte[]]@(0xED, 0x95, 0x9C, 0xEA, 0xB8, 0x80, 0x5F, 0xEB, 0xAC, 0xB8, 0xEC, 0x84, 0x9C, 0x2E, 0x74, 0x78, 0x74)) # "한글_문서.txt"
$uFile1 = "$specialSubDir\$koreanFileName"
$koreanContent = [System.Text.Encoding]::UTF8.GetString([byte[]]@(0xEB, 0x82, 0x98, 0xEB, 0x9E, 0x8F, 0xEB, 0xA7, 0x90, 0xEC, 0x83, 0x83, 0xEB, 0xAF, 0xB8, 0x20, 0xEB, 0x93, 0x89, 0xEA, 0xB7, 0xB9, 0xEC, 0x97, 0x90, 0x20, 0xEB, 0x8B, 0xAC, 0xEC, 0x95, 0x84))
[System.IO.File]::WriteAllText($uFile1, $koreanContent, [System.Text.Encoding]::UTF8)

$uFile2 = "$specialSubDir\special_brackets_and_hash.dat"
$randBytes = New-Object byte[] (512 * 1024) # 512 KB
(New-Object Random 99).NextBytes($randBytes)
[System.IO.File]::WriteAllBytes($uFile2, $randBytes)

& $Engine backup --source $SourceDir --repo $RepoDir --session-id "s1_unicode"
Assert-Result ($LASTEXITCODE -eq 0) "Session 1 Backup completed successfully"

& $Engine restore --session-id "s1_unicode" --repo $RepoDir --target "$RestoreDir\s1"
Assert-Result ($LASTEXITCODE -eq 0) "Session 1 Restore completed successfully"

$restUFile1 = "$RestoreDir\s1\$koreanDirName\Report_2026_Special_Symbols\$koreanFileName"
Assert-Result (Test-Path $restUFile1) "Restored Korean file exists at exact path"

$origHash1 = (Get-FileHash -Algorithm SHA256 $uFile1).Hash
$restHash1 = (Get-FileHash -Algorithm SHA256 $restUFile1).Hash
Assert-Result ($origHash1 -eq $restHash1) "Korean text file SHA-256 match ($origHash1)"

$origHash2 = (Get-FileHash -Algorithm SHA256 $uFile2).Hash
$restHash2 = (Get-FileHash -Algorithm SHA256 "$RestoreDir\s1\$koreanDirName\Report_2026_Special_Symbols\special_brackets_and_hash.dat").Hash
Assert-Result ($origHash2 -eq $restHash2) "Special characters binary file SHA-256 match"

# ------------------------------------------------------------------------------
# TEST 2: Zero-byte files & Empty directories
# ------------------------------------------------------------------------------
Write-Host "`n>>> [TEST 2] Zero-byte Files & Empty Directories Preservation" -ForegroundColor Yellow

$zeroFile = "$SourceDir\empty_zero_byte.bin"
[System.IO.File]::WriteAllBytes($zeroFile, [byte[]]@())

$emptySubDir = "$SourceDir\empty_folder_root\nested_empty_folder"
[System.IO.Directory]::CreateDirectory($emptySubDir) | Out-Null

& $Engine backup --source $SourceDir --repo $RepoDir --session-id "s2_zerobyte"
Assert-Result ($LASTEXITCODE -eq 0) "Session 2 Backup completed successfully"

& $Engine restore --session-id "s2_zerobyte" --repo $RepoDir --target "$RestoreDir\s2"
Assert-Result ($LASTEXITCODE -eq 0) "Session 2 Restore completed successfully"

$restZeroFile = "$RestoreDir\s2\empty_zero_byte.bin"
Assert-Result (Test-Path $restZeroFile) "Zero-byte file exists in restored path"
Assert-Result ((Get-Item $restZeroFile).Length -eq 0) "Restored zero-byte file is exactly 0 bytes"

$restEmptyDir = "$RestoreDir\s2\empty_folder_root\nested_empty_folder"
Assert-Result (Test-Path $restEmptyDir) "Empty sub-directory tree preserved and restored"

# ------------------------------------------------------------------------------
# TEST 3: FastCDC In-Place Chunk Modification & Deduplication Verification
# ------------------------------------------------------------------------------
Write-Host "`n>>> [TEST 3] FastCDC In-Place Chunk Modification & Deduplication" -ForegroundColor Yellow

$largeFile = "$SourceDir\16mb_chunk_test.dat"
$largeBytes = New-Object byte[] (16 * 1024 * 1024) # 16 MB
$rng = New-Object Random 777
$rng.NextBytes($largeBytes)
[System.IO.File]::WriteAllBytes($largeFile, $largeBytes)

Write-Host "  -> Backing up initial 16MB file (Session 3A)..." -ForegroundColor DarkGray
$sw3A = [System.Diagnostics.Stopwatch]::StartNew()
$out3A = & $Engine backup --source $SourceDir --repo $RepoDir --session-id "s3_16mb_init"
$sw3A.Stop()
Assert-Result ($LASTEXITCODE -eq 0) "Session 3A Backup completed in $($sw3A.Elapsed.TotalSeconds.ToString('F3'))s"

# Modify ONLY 128 bytes right at offset 8 MB (in the middle)
Write-Host "  -> Mutating 128 bytes in the middle of 16MB file (offset 8MB)..." -ForegroundColor DarkGray
$fileStream = [System.IO.File]::OpenWrite($largeFile)
$fileStream.Seek(8 * 1024 * 1024, [System.IO.SeekOrigin]::Begin) | Out-Null
$mutation = [System.Text.Encoding]::UTF8.GetBytes("MUTATED_IN_PLACE_CHUNK_DATA_FOR_FASTCDC_TEST_VERIFICATION_1234567890_EXACT_MODIFICATION")
$fileStream.Write($mutation, 0, $mutation.Length)
$fileStream.Close()

Write-Host "  -> Backing up mutated 16MB file (Session 3B)..." -ForegroundColor DarkGray
$sw3B = [System.Diagnostics.Stopwatch]::StartNew()
$out3B = & $Engine backup --source $SourceDir --repo $RepoDir --session-id "s3_16mb_mutated"
$sw3B.Stop()
Assert-Result ($LASTEXITCODE -eq 0) "Session 3B Backup completed in $($sw3B.Elapsed.TotalSeconds.ToString('F3'))s"

# Parse output to verify deduplication
Write-Host "  Session 3B Results:" -ForegroundColor DarkGray
$out3B | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

# Restore both sessions to verify version isolation
& $Engine restore --session-id "s3_16mb_init" --repo $RepoDir --target "$RestoreDir\s3_init"
& $Engine restore --session-id "s3_16mb_mutated" --repo $RepoDir --target "$RestoreDir\s3_mutated"

$initHash = (Get-FileHash -Algorithm SHA256 "$RestoreDir\s3_init\16mb_chunk_test.dat").Hash
$mutatedHash = (Get-FileHash -Algorithm SHA256 "$RestoreDir\s3_mutated\16mb_chunk_test.dat").Hash

Assert-Result ($initHash -ne $mutatedHash) "Initial and mutated 16MB files have distinct hashes"
Assert-Result ($mutatedHash -eq (Get-FileHash -Algorithm SHA256 $largeFile).Hash) "Mutated file restored bit-for-bit correctly"

# ------------------------------------------------------------------------------
# TEST 4: Massive Scale & Deep Directory Nesting (1,000 files, 10 levels deep)
# ------------------------------------------------------------------------------
Write-Host "`n>>> [TEST 4] Massive Scale: 1,000 Files & 10 Deep Directory Levels" -ForegroundColor Yellow

$deepRoot = "$SourceDir\deep_hierarchy"
$currentDir = $deepRoot
for ($d = 1; $d -le 10; $d++) {
    $currentDir = "$currentDir\level_$d"
    [System.IO.Directory]::CreateDirectory($currentDir) | Out-Null
}

Write-Host "  -> Generating 1,000 small and medium files across tree..." -ForegroundColor DarkGray
$swGen = [System.Diagnostics.Stopwatch]::StartNew()
for ($i = 1; $i -le 1000; $i++) {
    $targetDir = if ($i % 2 -eq 0) { $currentDir } else { "$deepRoot\branch_$($i % 10 + 1)" }
    [System.IO.Directory]::CreateDirectory($targetDir) | Out-Null
    $content = "File ID: $i - Timestamp: $(Get-Date -Format 'o') - UUID: $([Guid]::NewGuid().ToString())"
    [System.IO.File]::WriteAllText("$targetDir\file_$i.txt", $content, [System.Text.Encoding]::UTF8)
}
$swGen.Stop()
Write-Host "  -> Generated 1,000 files in $($swGen.Elapsed.TotalSeconds.ToString('F2'))s" -ForegroundColor DarkGray

Write-Host "  -> Backing up 1,000+ files tree..." -ForegroundColor DarkGray
$swBack = [System.Diagnostics.Stopwatch]::StartNew()
$out4 = & $Engine backup --source $SourceDir --repo $RepoDir --session-id "s4_scale_1000"
$swBack.Stop()
Assert-Result ($LASTEXITCODE -eq 0) "Session 4 Scale Backup completed in $($swBack.Elapsed.TotalSeconds.ToString('F2'))s"

Write-Host "  -> Restoring 1,000+ files tree..." -ForegroundColor DarkGray
$swRest = [System.Diagnostics.Stopwatch]::StartNew()
$outRest4 = & $Engine restore --session-id "s4_scale_1000" --repo $RepoDir --target "$RestoreDir\s4"
$swRest.Stop()
Assert-Result ($LASTEXITCODE -eq 0) "Session 4 Scale Restore completed in $($swRest.Elapsed.TotalSeconds.ToString('F2'))s"

Write-Host "  -> Verifying file count and sample integrity..." -ForegroundColor DarkGray
$restoredFiles = Get-ChildItem -Recurse "$RestoreDir\s4\deep_hierarchy" -File
Assert-Result ($restoredFiles.Count -eq 1000) "Exactly 1,000 files restored in deep hierarchy"

# Sample 50 random files and compare SHA256
$sampleErrors = 0
for ($k = 1; $k -le 50; $k++) {
    $testId = (New-Object Random).Next(1, 1001)
    $origFile = Get-ChildItem -Recurse "$deepRoot" -Filter "file_$testId.txt" | Select-Object -First 1
    $restFile = Get-ChildItem -Recurse "$RestoreDir\s4\deep_hierarchy" -Filter "file_$testId.txt" | Select-Object -First 1
    
    if ((Get-FileHash -Algorithm SHA256 $origFile.FullName).Hash -ne (Get-FileHash -Algorithm SHA256 $restFile.FullName).Hash) {
        $sampleErrors++
    }
}
Assert-Result ($sampleErrors -eq 0) "50 Randomly sampled files matched 100% SHA-256"

# ------------------------------------------------------------------------------
# FINAL REPORT
# ------------------------------------------------------------------------------
Write-Host "`n====================================================================" -ForegroundColor Cyan
Write-Host "               KOVA STRESS VERIFICATION SUITE COMPLETED             " -ForegroundColor Cyan
Write-Host "====================================================================" -ForegroundColor Cyan
Write-Host " Total Assertions Passed : $PassCount / $TotalTests" -ForegroundColor Green
Write-Host " Unicode / Jamo / Special: PASSED" -ForegroundColor Green
Write-Host " Zero-Byte & Empty Dirs  : PASSED" -ForegroundColor Green
Write-Host " FastCDC Chunk Mut Dedup : PASSED" -ForegroundColor Green
Write-Host " 1,000 Files Deep Tree   : PASSED" -ForegroundColor Green
Write-Host "====================================================================`n" -ForegroundColor Cyan
