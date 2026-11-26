# Install on Windows or Mac

1. Open the [latest release](https://github.com/spicythermal/jobalerts/releases/latest) and download:
   - Windows: `jobalerts-<version>-windows-setup.exe`
   - Mac with Apple silicon (M1 or newer): `jobalerts-<version>-mac-arm64.dmg`
   - Older Intel Mac: `jobalerts-<version>-mac-intel.dmg`

   To check which Mac you have: Apple menu → About This Mac → Chip (Apple M…) or Processor (Intel).

## Windows

2. Run the downloaded file. Windows shows "Windows protected your PC", because the installer isn't
   signed. Click **More info**, then **Run anyway**.
3. Follow the installer. No administrator password is needed. Leave "Start jobalerts" ticked and click
   **Finish**.

## Mac

2. Open the downloaded `.dmg` and drag **jobalerts** onto **Applications**.
3. Open Applications and double-click **jobalerts**. macOS says it can't verify the app, because it
   isn't signed. Click **Done** (not Move to Trash).
4. Open System Settings → **Privacy & Security**, scroll to Security, and click **Open Anyway** next to
   the jobalerts message. Confirm with your password, then click **Open**.

Steps 3–4 are needed only the first time.

## First start

Your browser opens jobalerts with the setup code filled in. Choose a password and click **Save and log
in**. Then follow [Setup](../README.md#setup) in the README.

jobalerts runs in the background and starts when you log in. Its icon is in the taskbar's notification
area (Windows) or the menu bar (Mac):

- **Open jobalerts:** opens it in the browser. Starting the app again does the same.
- **Start at login:** on or off.
- **Quit:** stops it until you start it again.

The address is `http://127.0.0.1:8765`; it works only on this computer. Phone pushes still work
(Settings → API keys → ntfy); they link to the employer's posting.

## Updates

Settings → Updates shows new versions. **Update now** downloads and installs the new version and
restarts jobalerts. Your data is kept.

## Your data

| Windows | Mac |
|---|---|
| `%LOCALAPPDATA%\jobalerts\data` | `~/Library/Application Support/jobalerts/data` |

The log is `jobalerts.log` in that folder. Uninstalling keeps it; delete the folder to remove
everything.

## Uninstall

- Windows: Settings → Apps → Installed apps → jobalerts → Uninstall.
- Mac: Quit from the menu-bar icon, delete jobalerts from Applications, and delete
  `~/Library/LaunchAgents/io.github.spicythermal.jobalerts.plist`.
