# Registers the watcher that serves the trained adapter, every 10 minutes for 14
# hours. Written as a file rather than a schtasks one-liner because the /TR value
# has to carry quoted paths and its own arguments, and that quoting is where these
# get silently mangled into a task that runs the wrong script.
$Copilot = "C:\Users\gameb\Documents\Books\Pipeline_copilot"
$Name    = "ForgeCopilotServe"
Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
  '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ' +
  "`"$Copilot\serve_when_trained.ps1`" -Out `"runs\sft-2b-v4`" " +
  '-Base "Qwen/Qwen3.5-2B" -Port 8099 -Eval')
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
  -RepetitionInterval (New-TimeSpan -Minutes 10) `
  -RepetitionDuration (New-TimeSpan -Hours 14)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
  -Settings $settings -Description "Serve the trained copilot LoRA when the fine-tune finishes" | Out-Null
$t = Get-ScheduledTask -TaskName $Name
"registered: $($t.TaskName) state=$($t.State)"
"next run: $((Get-ScheduledTaskInfo -TaskName $Name).NextRunTime)"
"action: $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
