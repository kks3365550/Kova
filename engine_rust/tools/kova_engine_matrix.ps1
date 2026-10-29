# KOVA Engine verification matrix: small virtual disks, many layouts.
# Per scenario: create VHDX -> NTFS -> fill -> VSS snapshot -> kova_engine write -> verify
#   -> restore to 2nd VHDX -> kova_engine compare -> mount + chkdsk + file-hash manifest diff
#   -> negative test (corrupt a copy of the image, verify must FAIL).
# Needs admin (VSS, diskpart); relaunches itself elevated. Only touches $Work and its own VHDX files.
param(
  [string]$Only = '',
  [string]$Work = 'D:\kova_matrix',
  [string]$Exe  = 'C:\Users\kksjmj\Desktop\ai\target_rust\release\kova_engine.exe',
  [int]$Repeat = 1,
  [switch]$PreTouch,
  [string]$ShadowOn = '',
  [switch]$Relaunched
)
$ErrorActionPreference = 'Continue'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  if ($Relaunched) { Write-Host 'Still not admin after relaunch; aborting (no loop).'; Start-Sleep 5; exit 1 }
  $a = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Only `"$Only`" -Work `"$Work`" -Exe `"$Exe`" -Repeat $Repeat -ShadowOn `"$ShadowOn`" $(if ($PreTouch) { '-PreTouch' }) -Relaunched"
  Start-Process powershell -Verb RunAs -Wait -ArgumentList $a
  exit 0
}

New-Item -ItemType Directory -Force $Work | Out-Null
$log = Join-Path $Work 'matrix.log'
Remove-Item $log -Force -ErrorAction SilentlyContinue
function L($m) { ("{0}  {1}" -f (Get-Date -Format HH:mm:ss), $m) | Add-Content $log -Encoding UTF8 }

# ---------- scenarios ----------
# fill: mixed | small | large | frag | empty | full
$scenarios = @(
  @{ name='gpt-4k-mixed';   mb=1024; style='GPT'; cluster=4096;  fill='mixed'; level=1 },
  @{ name='gpt-64k-large';  mb=1024; style='GPT'; cluster=65536; fill='large';      level=3 },
  @{ name='mbr-4k-small';   mb=768;  style='MBR'; cluster=4096;  fill='small';      level=1 },
  @{ name='gpt-4k-frag';    mb=1024; style='GPT'; cluster=4096;  fill='frag';       level=1 },
  @{ name='gpt-4k-empty';   mb=256;  style='GPT'; cluster=4096;  fill='empty';      level=1 },
  @{ name='gpt-4k-full';    mb=512;  style='GPT'; cluster=4096;  fill='full';       level=0 }
)
if ($Only) { $scenarios = $scenarios | Where-Object { $_.name -like $Only } }

# ---------- helpers ----------
function Invoke-Dp($text) {
  $f = Join-Path $env:TEMP ("kova_dp_{0}.txt" -f $PID)
  Set-Content $f $text -Encoding ASCII
  $o = diskpart /s $f 2>&1
  Remove-Item $f -Force -ErrorAction SilentlyContinue
  $o
}
function Attach-New($vhd, $mb, $style) {
  Invoke-Dp "create vdisk file=`"$vhd`" maximum=$mb type=expandable`r`nselect vdisk file=`"$vhd`"`r`nattach vdisk`r`n" | Out-Null
  $d = Get-DiskImage -ImagePath $vhd | Get-Disk
  Initialize-Disk -Number $d.Number -PartitionStyle $style
  $d.Number
}
function Detach($vhd) {
  if (Test-Path $vhd) { Invoke-Dp "select vdisk file=`"$vhd`"`r`ndetach vdisk`r`n" | Out-Null }
}
function Write-Data($path, [long]$bytes, [int]$seed, [string]$kind) {
  $rng = New-Object System.Random $seed
  $buf = New-Object byte[] (1MB)
  $fs = [IO.File]::Create($path)
  try {
    $left = $bytes
    while ($left -gt 0) {
      $n = [int][Math]::Min($left, $buf.Length)
      if ($kind -eq 'text') {
        for ($i = 0; $i -lt $n; $i++) { $buf[$i] = 65 + (($i + $seed) % 26) }
        $buf[$rng.Next($n)] = 0
      } else { $rng.NextBytes($buf) }
      $fs.Write($buf, 0, $n)
      $left -= $n
    }
  } finally { $fs.Dispose() }
}
function Get-Manifest($root) {
  $h = @{}
  $sha = [Security.Cryptography.SHA256]::Create()
  Get-ChildItem -LiteralPath $root -Recurse -Force -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notlike '*\System Volume Information\*' -and $_.FullName -notlike '*$RECYCLE.BIN*' } |
    ForEach-Object {
      $rel = $_.FullName.Substring($root.Length)
      try { $s = [IO.File]::OpenRead($_.FullName); $hash = [BitConverter]::ToString($sha.ComputeHash($s)); $s.Dispose() }
      catch { $hash = 'READERR' }
      $h[$rel] = "{0}|{1}" -f $_.Length, $hash
    }
  $h
}
function Populate($root, $fill, $mb) {
  switch ($fill) {
    'mixed' {
      New-Item -ItemType Directory -Force "${root}docs","${root}bin","${root}comp","${root}deep\a\b\c\d" | Out-Null
      1..300 | ForEach-Object { Write-Data "${root}docs\f$_.txt" (Get-Random -Minimum 100 -Maximum 40000) $_ 'text' }
      1..6   | ForEach-Object { Write-Data "${root}bin\r$_.bin" (Get-Random -Minimum 2MB -Maximum 30MB) (100 + $_) 'rand' }
      Write-Data "${root}deep\a\b\c\d\x.txt" 12345 7 'text'
      Write-Data "${root}empty.dat" 0 1 'text'
      # sparse file
      $sp = "${root}sparse.dat"; [IO.File]::Create($sp).Dispose()
      fsutil sparse setflag $sp | Out-Null
      $fs = [IO.File]::Open($sp, 'Open', 'ReadWrite'); $fs.SetLength(60MB); $fs.Position = 10MB
      $b = [byte[]](1..200 | ForEach-Object { $_ % 256 }); $fs.Write($b, 0, $b.Length); $fs.Dispose()
      # NTFS-compressed dir
      compact /c "${root}comp" | Out-Null
      1..20 | ForEach-Object { Write-Data "${root}comp\c$_.txt" 300000 $_ 'text' }
      # alternate data stream
      Set-Content -Path "${root}docs\f1.txt" -Stream 'meta' -Value 'ads-test'
    }
    'small' {
      1..40 | ForEach-Object { $d = "${root}d$_"; New-Item -ItemType Directory -Force $d | Out-Null
        1..100 | ForEach-Object { Write-Data "$d\s$_.txt" (Get-Random -Minimum 1 -Maximum 3000) ($_ * 3) 'text' } }
    }
    'large' {
      1..3 | ForEach-Object { Write-Data "${root}big$_.bin" 150MB (200 + $_) 'rand' }
      Write-Data "${root}bigtext.txt" 200MB 5 'text'
    }
    'frag' {
      New-Item -ItemType Directory -Force "${root}f" | Out-Null
      1..600 | ForEach-Object { Write-Data "${root}f\a$_.bin" 256KB $_ 'rand' }
      1..600 | Where-Object { $_ % 2 -eq 1 } | ForEach-Object { Remove-Item "${root}f\a$_.bin" -Force }
      1..30  | ForEach-Object { Write-Data "${root}f\big$_.bin" 2MB (900 + $_) 'rand' }
    }
    'empty' { Write-Data "${root}one.txt" 1000 1 'text' }
    'full' {
      $i = 0
      while ($true) {
        $free = (Get-PSDrive ($root.Substring(0,1))).Free
        if ($free -lt ($mb * 1MB * 0.14)) { break }
        $i++; Write-Data "${root}fill$i.bin" 4MB $i 'rand'
      }
    }
  }
}
function Run-Engine([string[]]$a, [string]$tag) {
  $out = & $Exe @a 2>&1 | ForEach-Object { "$_" }
  $code = $LASTEXITCODE
  $out | Where-Object { $_ -notmatch '\] (write|restore|compare|verify)' } | ForEach-Object { L ("  [{0}] {1}" -f $tag, ($_.Substring(0, [Math]::Min(220, $_.Length)))) }
  L ("  [{0}] exit {1}" -f $tag, $code)
  $code
}
function Letter-Of($disk, $part) {
  $p = Get-Partition -DiskNumber $disk -PartitionNumber $part
  if (-not $p.DriveLetter) { $p | Add-PartitionAccessPath -AssignDriveLetter; $p = Get-Partition -DiskNumber $disk -PartitionNumber $part }
  [string]$p.DriveLetter
}

# ---------- run ----------
$results = @()
foreach ($sc in $scenarios) {
 for ($rep = 1; $rep -le $Repeat; $rep++) {
  $tag = "{0}#{1}" -f $sc.name, $rep
  L "=================== $tag ==================="
  $srcVhd = Join-Path $Work "$($sc.name)_src.vhdx"
  $dstVhd = Join-Path $Work "$($sc.name)_dst.vhdx"
  $img    = Join-Path $Work "$($sc.name).kova"
  $bad    = Join-Path $Work "$($sc.name).bad.kova"
  Remove-Item $srcVhd,$dstVhd,$img,$bad -Force -ErrorAction SilentlyContinue
  $sid = $null
  $r = [ordered]@{ scenario=$tag; write=''; verify=''; restore=''; compare=''; chkdsk=''; files=''; negative=''; snap1=''; snap2=''; sec=0; result='FAIL' }
  $t0 = Get-Date
  try {
    # source volume
    $n1 = Attach-New $srcVhd $sc.mb $sc.style
    $p1 = New-Partition -DiskNumber $n1 -UseMaximumSize -AssignDriveLetter
    Format-Volume -Partition $p1 -FileSystem NTFS -AllocationUnitSize $sc.cluster -NewFileSystemLabel 'KSRC' -Confirm:$false -Force | Out-Null
    $s = Letter-Of $n1 $p1.PartitionNumber; $sroot = "${s}:\"
    L "src volume $sroot disk $n1 style $($sc.style) cluster $($sc.cluster) fill $($sc.fill)"
    Populate $sroot $sc.fill $sc.mb
    Write-VolumeCache $s
    $man1 = Get-Manifest $sroot
    L "source files: $($man1.Count)"

    # snapshot + write + verify
    if ($ShadowOn) {
      $vo = vssadmin add shadowstorage /for=${s}: /on=$ShadowOn /maxsize=2GB 2>&1 | Out-String
      L ("shadow storage -> $ShadowOn : " + ($vo -replace '\s+',' ').Trim().Substring(0,[Math]::Min(120,$vo.Length)))
    }
    $sr = (Get-WmiObject -List Win32_ShadowCopy).Create($sroot, 'ClientAccessible')
    if ($sr.ReturnValue -ne 0) { throw "snapshot failed $($sr.ReturnValue)" }
    $sid = $sr.ShadowID
    $dev = ((Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $sid }).DeviceObject).TrimEnd('\')
    if ($PreTouch) {
      try { [void][IO.Directory]::GetFileSystemEntries($dev + ''); L 'pre-touched snapshot (listed root)' } catch { L "pre-touch failed: $_" }
    }
    $c = Run-Engine @('write','--source',$dev,'--target',$img,'--threads','4','--chunk-mb','4','--level',"$($sc.level)") "write"
    $r.write = $c
    $r.verify = Run-Engine @('verify','--image',$img,'--threads','4') "verify"
    $r.snap1 = Run-Engine @('compare','--image',$img,'--source',$dev,'--target',$dev,'--threads','4') "snapcheck-after-write(info)"
    L ("image size: {0:N1} MB (volume {1} MB)" -f ((Get-Item $img).Length / 1MB), $sc.mb)

    # restore to second disk
    $n2 = Attach-New $dstVhd ($sc.mb + 64) $sc.style
    $p2 = New-Partition -DiskNumber $n2 -UseMaximumSize
    $tgt = '\\?\GLOBALROOT\Device\Harddisk' + $n2 + '\Partition' + $p2.PartitionNumber
    # strict byte check: restore to a plain sparse file (nothing mounts it), compare to the snapshot
    $rbin = Join-Path $Work "$($sc.name).restored.bin"
    $rf = Run-Engine @('restore','--image',$img,'--target',$rbin,'--threads','4') "restore-file"
    $r.compare = if ($rf -ne 0) { $rf } else { Run-Engine @('compare','--image',$img,'--source',$dev,'--target',$rbin,'--threads','4') "compare-file" }
    if ($r.compare -ne 0 -and (Test-Path $rbin)) {
      # dump the first differing bytes: restored file (= what was written) vs snapshot now
      try {
        Add-Type -TypeDefinition 'public static class KD { public static long[] Diff(byte[] a, byte[] b) { long first=-1,last=-1,n=0; for (int i=0;i<a.Length;i++){ if(a[i]!=b[i]){ if(first<0) first=i; last=i; n++; } } return new long[]{first,last,n}; } }' -ErrorAction SilentlyContinue
        $base = 351600640L; $len = 4194304
        $fa = [IO.File]::OpenRead($rbin); $fa.Position = $base; $ba = New-Object byte[] $len; [void]$fa.Read($ba,0,$len); $fa.Dispose()
        $fb = New-Object IO.FileStream($dev, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite, 4096, [IO.FileOptions]::None)
        $fb.Position = $base; $bb = New-Object byte[] $len; [void]$fb.Read($bb,0,$len); $fb.Dispose()
        $d = [KD]::Diff($ba,$bb)
        L ("  [diag] diff first={0} last={1} count={2} (offsets within chunk @ {3})" -f $d[0],$d[1],$d[2],$base)
        $o = [int]($d[0] - ($d[0] % 16))
        L ("  [diag] written : " + (($ba[$o..($o+47)] | ForEach-Object { $_.ToString('x2') }) -join ''))
        L ("  [diag] snapshot: " + (($bb[$o..($o+47)] | ForEach-Object { $_.ToString('x2') }) -join ''))
        $asc = { param($x) -join ($x | ForEach-Object { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { '.' } }) }
        L ("  [diag] written  ascii: " + (& $asc $ba[$o..($o+47)]))
        L ("  [diag] snapshot ascii: " + (& $asc $bb[$o..($o+47)]))
        $lo = [int]$d[1] - 63
        L ("  [diag] snapshot @last ascii: " + (& $asc $bb[$lo..($lo+63)]))
      } catch { L "  [diag] dump failed: $_" }
    }
    Remove-Item $rbin -Force -ErrorAction SilentlyContinue
    # device restore (what a real restore does): then mount, chkdsk, file hashes. Byte compare here is informational
    # because Windows may mount the volume and write to it right after the restore.
    $r.restore = Run-Engine @('restore','--image',$img,'--target',$tgt,'--threads','4') "restore-dev"
    $cd = Run-Engine @('compare','--image',$img,'--source',$dev,'--target',$tgt,'--threads','4') "compare-dev(info)"

    # independent check: mount restored volume, chkdsk, file hashes
    Update-Disk -Number $n2; Start-Sleep 2
    $d = Letter-Of $n2 $p2.PartitionNumber
    if ($d) {
      $droot = "${d}:\"
      $fsn = (Get-Volume -DriveLetter $d).FileSystem
      L "restored volume $droot fs=$fsn"
      $ck = chkdsk "${d}:" 2>&1 | ForEach-Object { "$_" }
      $r.chkdsk = $LASTEXITCODE
      $ck | Select-Object -Last 8 | ForEach-Object { L "  [chkdsk] $_" }
      $man2 = Get-Manifest $droot
      $diff = 0
      foreach ($k in $man1.Keys) { if ($man2[$k] -ne $man1[$k]) { $diff++; if ($diff -le 5) { L "  DIFF $k : $($man1[$k]) vs $($man2[$k])" } } }
      foreach ($k in $man2.Keys) { if (-not $man1.ContainsKey($k)) { $diff++; L "  EXTRA $k" } }
      $r.files = "$($man1.Count) files, $diff diff"
      L "manifest: $($r.files)"
      $filesOk = ($diff -eq 0)
    } else { $r.files = 'no drive letter'; $filesOk = $false }

    # negative test: corrupt a copy of the image, verify must fail
    Copy-Item $img $bad -Force
    $fs = [IO.File]::Open($bad, 'Open', 'ReadWrite')
    $pos = [long]($fs.Length / 3); $fs.Position = $pos
    $b = New-Object byte[] 1; [void]$fs.Read($b, 0, 1); $fs.Position = $pos; $fs.WriteByte([byte]($b[0] -bxor 0xFF)); $fs.Dispose()
    $nc = Run-Engine @('verify','--image',$bad,'--threads','4') "verify-corrupt"
    $r.negative = if ($nc -ne 0) { 'detected' } else { 'MISSED' }
    $r.snap2 = Run-Engine @('compare','--image',$img,'--source',$dev,'--target',$dev,'--threads','4') "snapcheck-at-end(info)"

    $ok = ($r.write -eq 0) -and ($r.verify -eq 0) -and ($r.restore -eq 0) -and ($r.compare -eq 0) -and ($r.chkdsk -eq 0) -and $filesOk -and ($nc -ne 0)
    if ($ok) { $r.result = 'PASS' }
  }
  catch { L "EXCEPTION: $_" }
  finally {
    if ($sid) { try { (Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $sid }).Delete() } catch {} }
    if ($ShadowOn -and $s) { vssadmin delete shadowstorage /for=${s}: /quiet 2>&1 | Out-Null }
    Detach $dstVhd; Detach $srcVhd
    Remove-Item $srcVhd,$dstVhd,$img,$bad -Force -ErrorAction SilentlyContinue
    $r.sec = [int]((Get-Date) - $t0).TotalSeconds
    L ("RESULT {0}: {1} ({2}s)" -f $tag, $r.result, $r.sec)
    $results += [pscustomobject]$r
  }
 }
}
$results | Export-Csv (Join-Path $Work 'results.csv') -NoTypeInformation -Encoding UTF8
L '--- SUMMARY ---'
$results | ForEach-Object { L ("{0,-22} {1}  write={2} verify={3} restore={4} compare={5} chkdsk={6} files=[{7}] neg={8} snap1={10} snap2={11} {9}s" -f $_.scenario,$_.result,$_.write,$_.verify,$_.restore,$_.compare,$_.chkdsk,$_.files,$_.negative,$_.sec,$_.snap1,$_.snap2) }
L 'matrix done'
