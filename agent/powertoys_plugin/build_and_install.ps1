# Сборка и установка плагина PowerToys Run «Ассистент» (ключевое слово @).
# Компилирует против библиотек самого PowerToys (они на .NET 10), поэтому достаточно .NET SDK 8:
# компилятор берётся из него, а справочные сборки из папки PowerToys.
# После установки перезапусти PowerToys (трей → Exit, затем запусти снова).
$ErrorActionPreference = "Stop"
$pt   = Join-Path $env:LOCALAPPDATA "PowerToys"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$csc  = Get-ChildItem "C:\Program Files\dotnet\sdk" -Recurse -Filter csc.dll | Select-Object -First 1 -ExpandProperty FullName
if (-not $csc) { throw ".NET SDK не найден (нужен любой, начиная с 8)" }
$name = "Community.PowerToys.Run.Plugin.Assistant"
$dest = Join-Path $env:LOCALAPPDATA "Microsoft\PowerToys\PowerToys Run\Plugins\Assistant"
$refs = "System.Private.CoreLib","System.Private.Uri","System.Runtime","System.Net.Http","System.Text.Json",
        "System.Collections","System.Linq","System.Threading.Tasks","System.Threading","Wox.Plugin","Wox.Infrastructure",
        "PowerToys.ManagedCommon","PowerToys.Common.UI","PresentationFramework","PresentationCore","WindowsBase",
        "System.Xaml","System.ObjectModel","System.ComponentModel.Primitives","System.Runtime.InteropServices",
        "System.Memory","netstandard","System.Text.Encoding.Extensions","System.Console" | ForEach-Object { "-r:$pt\$_.dll" }
New-Item -ItemType Directory -Force $dest | Out-Null
& dotnet $csc -nostdlib -nologo -target:library -nullable:disable -langversion:latest "-out:$dest\$name.dll" "$here\Main.cs" @refs
if ($LASTEXITCODE -ne 0) { throw "сборка не удалась" }
Copy-Item "$here\plugin.json" $dest -Force
Copy-Item "$here\Images" $dest -Recurse -Force
"Установлено в: $dest`nПерезапусти PowerToys, затем в строке Run набери:  @ привет"
