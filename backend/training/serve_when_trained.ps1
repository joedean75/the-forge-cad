# Bring the copilot model up as soon as training finishes, then remove this task.
# Independent of any Claude session: if training ends unattended, the endpoint is
# there to be evaluated against.
$Root    = "C:\Users\gameb\Documents\Books"
$Copilot = "$Root\Pipeline_copilot"
$Python  = "$Root\How_to_make_a_reasoning_model_from_scratch\My_work\.venv\Scripts\python.exe"
$Adapter = "$Copilot\runs\sft\lora-final.pt"
$Log     = "$Copilot\serve_watch.log"
$Stamp   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

if (-not (Test-Path $Adapter)) {
  Add-Content $Log "$Stamp waiting: no lora-final.pt yet (training still running)"
  exit 0
}
if (Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like "*copilot_sft.py*" }) {
  Add-Content $Log "$Stamp waiting: the trainer is still writing"
  exit 0
}
if (Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like "*serve.py*" }) {
  Add-Content $Log "$Stamp already serving"
  schtasks /Delete /TN "ForgeCopilotServe" /F | Out-Null
  exit 0
}
$args = @("$Copilot\serve.py",
  "--weights","$Root\How_to_make_a_reasoning_model_from_scratch\qwen3\qwen3-0.6B-base.pth",
  "--package","$Root\How_to_make_a_reasoning_model_from_scratch\reasoning-from-scratch",
  "--lora",$Adapter, "--host","0.0.0.0","--port","8099","--max-new-tokens","512")
Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $Copilot `
  -RedirectStandardOutput "$Copilot\serve.log" -RedirectStandardError "$Copilot\serve.err" `
  -WindowStyle Hidden
Add-Content $Log "$Stamp started serve.py on the GPU with the trained adapter"
schtasks /Delete /TN "ForgeCopilotServe" /F | Out-Null
