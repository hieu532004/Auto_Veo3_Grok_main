[Setup]
AppName=Auto Veo3 Grok HieuMMO
AppVersion=4.0
AppPublisher=HieuMMO
DefaultDirName={autopf}\Auto Veo3 Grok HieuMMO
DefaultGroupName=Auto Veo3 Grok HieuMMO
OutputDir=.\Inno_Output
OutputBaseFilename=Auto_Veo3_Grok_Setup
Compression=lzma2/ultra64
SolidCompression=yes
SetupIconFile=app_icon.ico
DisableProgramGroupPage=yes
PrivilegesRequired=lowest

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\Auto_Veo3_Grok_HieuMMO.exe"; DestDir: "{app}"; Flags: ignoreversion
; Include other required folders or configurations if necessary
; Source: "Workflows\*"; DestDir: "{app}\Workflows"; Flags: ignoreversion recursesubdirs createallsubdirs
; Source: "app_icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Auto Veo3 Grok HieuMMO"; Filename: "{app}\Auto_Veo3_Grok_HieuMMO.exe"; IconFilename: "{app}\Auto_Veo3_Grok_HieuMMO.exe"
Name: "{autodesktop}\Auto Veo3 Grok HieuMMO"; Filename: "{app}\Auto_Veo3_Grok_HieuMMO.exe"; Tasks: desktopicon; IconFilename: "{app}\Auto_Veo3_Grok_HieuMMO.exe"

[Run]
Filename: "{app}\Auto_Veo3_Grok_HieuMMO.exe"; Description: "{cm:LaunchProgram,Auto Veo3 Grok HieuMMO}"; Flags: nowait postinstall skipifsilent
