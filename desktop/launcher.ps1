# Preflop Trainer launcher
# GTO server (8675) + Mirror server (8676) ab PreflopTrainer.exe ke andar hi chalte hain —
# yahan se alag python servers mat chalao, warna app band hote hi solver disconnect hota tha.
$root = "C:\Users\rohit\PREFLOP TRAINER\preflop-trainer"

# App pehle se khula ho to dusri copy mat kholo
if (Get-Process PreflopTrainer -ErrorAction SilentlyContinue) { exit }

Start-Process -FilePath "$root\desktop\dist\PreflopTrainer.exe" `
    -WorkingDirectory "$root\desktop\dist"
