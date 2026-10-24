#define AppVersion "0.2.79"
#ifndef AppExePath
  #define AppExePath "dist\FlowAgentConsoleInstalled\FlowAgentConsole.exe"
#endif
#ifndef AppRuntimeRoot
  #define AppRuntimeRoot "dist\FlowAgentConsoleInstalled\app-runtime"
#endif
#ifndef InstallerOutputDir
  #define InstallerOutputDir "dist\installer"
#endif
#ifndef BrowserRoot
  #error BrowserRoot must point to a Chrome for Testing chrome-win64 directory.
#endif

[Setup]
AppId={{8E25E8E2-2E1A-4D62-9A0C-E72DD98B8500}
AppName=Flow Agent 中文管理后台
AppVersion={#AppVersion}
AppVerName=Flow Agent 中文管理后台 {#AppVersion}
DefaultDirName={localappdata}\Programs\FlowAgentConsole
DefaultGroupName=Flow Agent 中文管理后台
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#InstallerOutputDir}
OutputBaseFilename=FlowAgentConsole_Setup_{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Flow Agent 中文管理后台
UninstallDisplayIcon={app}\FlowAgentConsole.exe
CloseApplications=yes
CloseApplicationsFilter=FlowAgentConsole.exe,FlowConsoleMCP.exe
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他选项："; Flags: unchecked

[Files]
Source: "{#AppRuntimeRoot}\*"; DestDir: "{app}\app-runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\FlowConsoleMCP\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Stop-FlowConsole.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppExePath}"; DestDir: "{app}"; DestName: "FlowAgentConsole.exe"; Flags: ignoreversion
Source: "{#BrowserRoot}\*"; DestDir: "{app}\browser\chrome-win64"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Flow Agent 中文管理后台"; Filename: "{app}\FlowAgentConsole.exe"; WorkingDir: "{app}"
Name: "{autoprograms}\Flow Agent 局域网 API"; Filename: "{app}\FlowAgentConsole.exe"; Parameters: "--lan"; WorkingDir: "{app}"
Name: "{autoprograms}\Flow Agent 后台静默运行"; Filename: "{app}\FlowAgentConsole.exe"; Parameters: "--no-browser"; WorkingDir: "{app}"
Name: "{autoprograms}\停止 Flow Agent 后台服务"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\Stop-FlowConsole.ps1"" -AppPath ""{app}\FlowAgentConsole.exe"""; WorkingDir: "{app}"; Flags: runminimized
Name: "{autodesktop}\Flow Agent 中文管理后台"; Filename: "{app}\FlowAgentConsole.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\FlowAgentConsole.exe"; Description: "安装完成后运行 Flow Agent 中文管理后台"; Flags: nowait postinstall skipifsilent unchecked

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
  Params: String;
begin
  Result := '';
  ExtractTemporaryFile('Stop-FlowConsole.ps1');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\Stop-FlowConsole.ps1') + '" -AppPath "' +
    ExpandConstant('{app}\FlowAgentConsole.exe') + '"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Params, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    Result := '无法运行应用关闭程序，请关闭 Flow Agent 后重试。'
  else begin
    if ExitCode = 5 then begin
      { Elevate only the scoped helper when the old application is elevated. }
      if not ShellExec('runas', ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
        Params, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
        ExitCode := 5;
    end;
    if ExitCode = 3 then
      Result := '后台有排队或生成任务，请先等待完成或取消排队任务，再安装升级。'
    else if ExitCode <> 0 then
      Result := '未能安全关闭旧版。请允许 Windows 的管理员权限提示，或先手动关闭旧版后重试。关闭诊断见用户数据目录 upgrade-close.log。';
  end;
end;

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "FlowAgentConsole"; Flags: uninsdeletevalue




