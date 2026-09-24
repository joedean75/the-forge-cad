# Starts a copilot fine-tune when the machine is free, then removes the trigger.
#
# Two conditions, both required:
#   (a) no python process is running a pipeline `run.py` — keyed on the command
#       line, not a PID, so restarting that pipeline (to skip epochs, say) still
#       holds this back;
#   (b) the GPU has at least $MinFreeMiB free — LM Studio alone can hold enough
#       to out-of-memory a fresh job even when nothing is training.
#
# **The run's arguments are parameters, not literals.** They were literals, and
# the defaults describe the 0.6B run: a 2B launched through them would have
# resumed from `runs\sft\latest.pt`, which holds a 0.6B adapter set, and trained
# at `--max-len 4096`, which that card cannot reach for a 2B. A launcher whose
# arguments belong to one base is a launcher that silently runs the wrong job.
param(
  [string]$Out        = "runs\sft",
  [string]$Base       = "",          # a Hugging Face id (--hf); empty = the book .pth
  [string]$Dataset    = "dataset",
  [int]   $MaxLen     = 4096,
  [int]   $Epochs     = 2,
  [int]   $Accumulate = 8,
  [double]$Lr         = 1e-4,
  [int]   $LogEvery   = 25,
  [int]   $ValSize    = 32,
  [int]   $CheckpointEvery = 100,
  [int]   $MaxSteps   = 0,          # 0 = the whole epoch

  [switch]$Resume,
  [switch]$NoSchedule                # launching by hand: leave the task alone
)
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

Add-Content $Log "$Stamp starting: GPU has ${free} MiB free; out=$Out base=$(if($Base){$Base}else{'0.6B .pth'}) max_len=$MaxLen"
$args = @(
  "$Copilot\copilot_sft.py",
  "--train","$Copilot\$Dataset\train.jsonl",
  "--val","$Copilot\$Dataset\val.jsonl",
  "--out","$Copilot\$Out",
  "--max-len","$MaxLen","--epochs","$Epochs","--lr","$Lr",
  "--accumulate","$Accumulate",
  "--log-every","$LogEvery","--val-size","$ValSize",
  "--checkpoint-every","$CheckpointEvery"
)
# The samples are shuffled before the loop, so a step bound is a representative
# subset of the mix rather than a prefix of it — which is what makes bounding a
# 16-hour epoch to one night an honest sample instead of "all the library ones".
if ($MaxSteps -gt 0) { $args += @("--max-steps","$MaxSteps") }
if ($Base) {
  $args += @("--hf",$Base)
} else {
  $args += @("--weights","$Root\How_to_make_a_reasoning_model_from_scratch\qwen3\qwen3-0.6B-base.pth",
             "--package","$Root\How_to_make_a_reasoning_model_from_scratch\reasoning-from-scratch")
}
if ($Resume) { $args += "--resume" }

# The allocator fragments on a card this close to full: 3,072 tokens peaks at
# 5.19 GiB of 5.94 free, and a 700 MiB margin is one badly-placed block wide.
$env:PYTORCH_CUDA_ALLOC_CONF = "expandable_segments:True"
Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $Copilot `
  -RedirectStandardOutput "$Copilot\train.log" -RedirectStandardError "$Copilot\train.err" `
  -WindowStyle Hidden
Add-Content $Log "$Stamp launched; logging to train.log"
if (-not $NoSchedule) {
  schtasks /Delete /TN "ForgeCopilotSFT" /F | Out-Null
  Add-Content $Log "$Stamp trigger removed (the run is on its own now)"
}
