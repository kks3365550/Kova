$hd = (Get-VM).HardDrives
foreach ($d in $hd) {
    $item = Get-Item $d.Path
    Write-Host "Path: $($item.FullName)"
    Write-Host "Size: $([math]::Round($item.Length / 1GB, 2)) GB"
    Write-Host "Modified: $($item.LastWriteTime)"
}
