# check_vhd_expand.ps1
$ErrorActionPreference = 'Continue'

$vm = Get-VM | Select-Object -First 1
Write-Host "VM: $($vm.Name)"

Write-Host "`n[Checkpoints]"
Get-VMSnapshot -VMName $vm.Name | Format-List Name, SnapshotType, CreationTime, ParentSnapshotName

Write-Host "`n[Hard Drives]"
$vm.HardDrives | Format-List ControllerType, ControllerNumber, Path

$vhdPath = $vm.HardDrives[0].Path
Write-Host "`n[VHD Details: $vhdPath]"
Get-VHD -Path $vhdPath | Format-List Path, VhdType, VhdFormat, Size, FileSize, ParentPath
