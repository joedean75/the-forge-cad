# Bring the copilot model up as soon as training finishes, then remove this task.
# Independent of any Claude session: if training ends unattended, the endpoint is
# there to be evaluated against.
#
# **The run is a parameter, like the trainer's.** These were literals naming the
# 0.6B run and `runs\sft`, so the first job that trained anything else would have
# been followed by a watcher serving the *previous* model — with a real endpoint
# answering, which is worse than no endpoint at all.
param(
  [string]$Out  = "runs\sft",
  [string]$Base = "",              # a Hugging Face id (--hf); empty = the book .pth
  [int]   $Port = 8099,
  # The served name reaches the eval artifacts and the copilot's own metadata, so
  # a 2B advertising itself as `forge-copilot-qwen3-0.6b` — the server's default —
  # mislabels every result it produces.
  [string]$ModelName = "",
  # Score the suite as soon as the endpoint answers. The measurement is the
  # reason the endpoint exists, and a checkpoint nobody pointed at anything is
  # the state the last two runs sat in overnight.
  [switch]$Eval,
  [string]$Distro = "Ubuntu"
)
$Root    = "C:\Users\gameb\Documents\Books"
$Copilot = "$Root\Pipeline_copilot"
$Python  = "$Root\How_to_make_a_reasoning_model_from_scratch\My_work\.venv\Scripts\python.exe"
$Adapter = "$Copilot\$Out\lora-final.pt"
$Done    = "$Copilot\$Out\summary.json"
$Log     = "$Copilot\serve_watch.log"
$Stamp   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# `summary.json` is the readiness signal rather than the adapter, because the
# trainer writes the adapter first: a watcher that fires on `lora-final.pt`
# alone can load a file that is still being flushed. The summary is written
# after it, so its existence means the adapter is closed.
if (-not (Test-Path $Done)) {
  Add-Content $Log "$Stamp waiting: no summary.json in $Out yet (training still running)"
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
if (-not (Test-Path $Adapter)) {
  Add-Content $Log "$Stamp refusing: $Out has a summary but no lora-final.pt"
  exit 1
}
$args = @("$Copilot\serve.py", "--lora",$Adapter,
          "--host","0.0.0.0","--port","$Port","--max-new-tokens","512")
if (-not $ModelName -and $Base) {
  $ModelName = "forge-copilot-" + ($Base -replace '.*/','').ToLower()
}
if ($ModelName) { $args += @("--model-name",$ModelName) }
if ($Base) {
  $args += @("--hf",$Base)
} else {
  $args += @("--weights","$Root\How_to_make_a_reasoning_model_from_scratch\qwen3\qwen3-0.6B-base.pth",
             "--package","$Root\How_to_make_a_reasoning_model_from_scratch\reasoning-from-scratch")
}
Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $Copilot `
  -RedirectStandardOutput "$Copilot\serve.log" -RedirectStandardError "$Copilot\serve.err" `
  -WindowStyle Hidden
Add-Content $Log "$Stamp started serve.py on the GPU with $Out\lora-final.pt (base: $(if($Base){$Base}else{'0.6B .pth'}))"
if ($Eval) {
  # Detached, because the suite takes far longer than this task's time limit:
  # the script waits for the endpoint itself rather than assuming it is up.
  $bash = "/home/joed/cad/backend/training/eval_when_served.sh"
  $launch = if ($ModelName) { "MODEL='$ModelName' $bash" } else { $bash }
  # **The command has to arrive as one argument.** `Start-Process` joins
  # `-ArgumentList` with spaces, so an unquoted string here reached `bash -lc` as
  # several arguments: bash took `MODEL=...` as the whole command, ran a no-op
  # assignment and exited 0. Nothing failed, no log, and no suite — which is the
  # worst shape a scheduled step can have.
  Start-Process -FilePath "wsl.exe" -WindowStyle Hidden `
    -ArgumentList @("-d",$Distro,"--","bash","-lc","`"$launch`"")
  Add-Content $Log "$Stamp launched the goal suite in WSL ($Distro)"
}
schtasks /Delete /TN "ForgeCopilotServe" /F | Out-Null
