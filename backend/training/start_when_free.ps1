# Starts the copilot fine-tune when the machine is free, then removes itself.
#
# Two conditions, both required:
#   (a) no python process is running Pipeline_claude\run.py — keyed on the command
#       line, not a PID, so restarting that pipeline (to skip epochs, say) still
#       holds this back;
#   (b) the GPU has at least $MinFreeMiB free — LM Studio alone can hold enough
#       to out-of-memory a fresh job even when nothing is training.
$Root      = "C:\Users\gameb\Documents\Books"
$Copilot   = "$Root\Pipeline_copilot"
$Python    = "$Root\How_to_make_a_reasoning_model_from_scratch\My_work\.venv\Scripts\python.exe"
$MinFreeMiB = 5000
$Log       = "$Copilot\watch.log"
$Stamp     = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Any python running a pipeline `run.py`, however it was invoked. The first
# version keyed on "Pipeline_claude\run.py" and missed the follow-up run, which
# passes a relative `run.py` — it would have launched this job on top of a GRPO
# run, and only LM Studio holding VRAM prevented it.
$busy = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
        Where-Object { $_.CommandLine -match '(^|[\\/ ])run\.py' -and
                       $_.CommandLine -notlike "*copilot_sft.py*" }
if ($busy) {
  Add-Content $Log "$Stamp waiting: a pipeline is running (PID $($busy.ProcessId -join ','))"
  exit 0
}
# Already training? Never start a second one.
$mine = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
        Where-Object { $_.CommandLine -like "*copilot_sft.py*" }
if ($mine) {
  Add-Content $Log "$Stamp waiting: the fine-tune is already running (PID $($mine.ProcessId -join ','))"
  exit 0
}
$free = (& nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits) -as [int]
$util = (& nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits) -as [int]
if ($free -lt $MinFreeMiB) {
  Add-Content $Log "$Stamp waiting: only ${free} MiB free on the GPU (need $MinFreeMiB; close LM Studio)"
  exit 0
}
# Free memory is not the same as an idle card: something can be busy in a small
# footprint, and a fine-tune sharing the GPU with generation is slower than
# either alone.
if ($util -gt 40) {
  Add-Content $Log "$Stamp waiting: GPU is ${util}% busy with something else"
  exit 0
}

Add-Content $Log "$Stamp starting: GPU has ${free} MiB free"
$args = @(
  "$Copilot\copilot_sft.py",
  "--train","$Copilot\dataset\train.jsonl",
  "--val","$Copilot\dataset\val.jsonl",
  "--out","$Copilot\runs\sft",
  "--weights","$Root\How_to_make_a_reasoning_model_from_scratch\qwen3\qwen3-0.6B-base.pth",
  "--package","$Root\How_to_make_a_reasoning_model_from_scratch\reasoning-from-scratch",
  "--max-len","4096","--epochs","2","--lr","1e-4",
  "--log-every","25","--checkpoint-every","100","--resume"
)
Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $Copilot `
  -RedirectStandardOutput "$Copilot\train.log" -RedirectStandardError "$Copilot\train.err" `
  -WindowStyle Hidden
Add-Content $Log "$Stamp launched; logging to train.log"
schtasks /Delete /TN "ForgeCopilotSFT" /F | Out-Null
Add-Content $Log "$Stamp trigger removed (the run is on its own now)"
