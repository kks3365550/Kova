# tools/capture_vm_screen.ps1
param(
    [string]$VmName = "USB_Rescue_Test",
    [string]$OutBmp = "C:\HyperV_USB_Test\vm_screen.bmp"
)

$ErrorActionPreference = "Stop"
$vsms = Get-CimInstance -Namespace root/virtualization/v2 -ClassName Msvm_VirtualSystemManagementService
$vm = Get-CimInstance -Namespace root/virtualization/v2 -ClassName Msvm_ComputerSystem | Where-Object { $_.ElementName -eq $VmName }

if ($vm) {
    $res = Invoke-CimMethod -InputObject $vsms -MethodName "GetVirtualSystemThumbnailImage" -Arguments @{
        TargetSystem = $vm
        WidthPixels = [uint16]800
        HeightPixels = [uint16]600
    }
    if ($res -and $res.ImageData) {
        [System.IO.File]::WriteAllBytes($OutBmp, $res.ImageData)
        Write-Host "SAVED:$($res.ImageData.Length)"
    } else {
        Write-Host "NO_IMAGE:$($res.ReturnValue)"
    }
} else {
    Write-Host "VM_NOT_FOUND"
}
