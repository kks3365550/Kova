$ErrorActionPreference='Continue'
$out='D:\kova_probe\out.txt'; '' | Out-File $out
function L($m){ $m | Out-File $out -Append -Encoding utf8 }
$v='D:\kova_probe\p.vhdx'
@"
create vdisk file=$v maximum=1024 type=expandable
select vdisk file=$v
attach vdisk
convert gpt
create partition primary
format fs=ntfs unit=65536 quick label=PROBE
assign letter=P
"@ | Set-Content D:\kova_probe\dp.txt -Encoding ascii
L (diskpart /s D:\kova_probe\dp.txt | Out-String)
Start-Sleep 2
$b=New-Object byte[] (200MB); (New-Object Random 1).NextBytes($b); [IO.File]::WriteAllBytes('P:\a.bin',$b)
$r=([wmiclass]'Win32_ShadowCopy').Create('P:\','ClientAccessible'); L "snap rc=$($r.ReturnValue)"
L "--- shadowstorage"; L (vssadmin list shadowstorage | Out-String)
L "--- layout SVI"; L (fsutil file layout 'P:\System Volume Information' 2>&1 | Out-String)
L "--- dir SVI"; L (cmd /c 'dir /a P:\"System Volume Information"' 2>&1 | Out-String)
L "--- lcn 5376"; L (fsutil file queryfileidbylcn 2>&1 | Out-String)
L "--- shadow cluster info"; L (fsutil volume allocationreport P: 2>&1 | Out-String)
L (vssadmin delete shadows /for=P: /all /quiet | Out-String)
"select vdisk file=$v`r`ndetach vdisk" | Set-Content D:\kova_probe\dp2.txt -Encoding ascii
L (diskpart /s D:\kova_probe\dp2.txt | Out-String)
Remove-Item $v -Force
L done
