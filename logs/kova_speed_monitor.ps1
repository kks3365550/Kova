$log='C:\Users\kksjmj\Desktop\ai\백업시스템\logs\kova_speed2_20261030.log'
$prevB=0;$prevT=Get-Date;$t0=$prevT
"[{0}] monitor start, cpu={1}" -f (Get-Date -f s),[Environment]::ProcessorCount | Out-File $log -Encoding utf8
while($true){
  Start-Sleep 30
  try{ $s=Invoke-RestMethod 'http://127.0.0.1:8765/api/system-image/status?target_drive=E:' -TimeoutSec 15 }catch{ continue }
  $f=Get-ChildItem E:\KovaImages -File -ErrorAction SilentlyContinue | ? { $_.Name -like "C_*" -and $_.Name -notlike "*.json" } | Sort LastWriteTime -Desc | Select -First 1
  $b=0; if($f){ try{ $fs=[IO.File]::Open($f.FullName,'Open','Read','ReadWrite,Delete'); $b=$fs.Length; $fs.Close() }catch{ $b=$f.Length } }; $now=Get-Date
  $mbs=[math]::Round(($b-$prevB)/1MB/($now-$prevT).TotalSeconds,1)
  $cpu=(Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction SilentlyContinue).CounterSamples.CookedValue
  "[{0}] elapsed={1}s pct={2} phase={3} image={4}MB write={5}MB/s cpu={6}%" -f (Get-Date -f s),[int]($now-$t0).TotalSeconds,$s.progress_percent,$s.current_phase,[int]($b/1MB),$mbs,[int]$cpu | Add-Content $log -Encoding utf8
  $prevB=$b;$prevT=$now
  if(-not $s.is_running){ "[{0}] backup finished status={1}" -f (Get-Date -f s),$s.status | Add-Content $log -Encoding utf8; break }
}
