# Install on Windows

The desktop app is for Windows. On a Mac or Linux, run jobalerts with Python or Docker
([README](../README.md#quick-start)).

1. Open the [latest release](https://github.com/spicythermal/jobalerts/releases/latest) and download
   `jobalerts-<version>-windows-setup.exe`.
2. Run the downloaded file. Windows shows "Windows protected your PC", because the installer isn't
   signed. Click **More info**, then **Run anyway**.
3. Follow the installer. No administrator password is needed. Leave "Start jobalerts" ticked and click
   **Finish**.

## First start

Your browser opens jobalerts with the setup code filled in. Keep the username `admin` or choose
another, choose a password, and choose whether other users may use your Anthropic API key. Click
**Save and log in**. Then follow [Setup](../README.md#setup) in the README.

jobalerts runs in the background and starts when you log in. Its icon is in the taskbar's notification
area:

- **Open jobalerts:** opens it in the browser. Starting the app again does the same.
- **Start at login:** on or off.
- **Quit:** stops it until you start it again.

The address is `http://127.0.0.1:8765`; it works only on this computer. Phone pushes still work
(Settings → API keys → ntfy); they link to the employer's posting.

## Other users

Settings → Users → **Add a user** makes an account and shows a one-time password to give them. Each
user has their own jobs, filter, stories, profile, resumes and settings.

To let them use it from other computers on your network: Settings → Connections → "Desktop app:
other computers on this network can open it", then Quit and start jobalerts again. Settings → Users
shows the address to open. Windows asks once whether to allow jobalerts through the firewall: allow
it on private networks.

## Updates

Settings → Updates shows new versions. **Update now** downloads the new version, checks its
signature, installs it and restarts jobalerts. Your data is kept.

## Your data

`%LOCALAPPDATA%\jobalerts\data`. The log is `jobalerts.log` in that folder. Uninstalling keeps it; delete the folder to remove
everything.

## Uninstall

Settings → Apps → Installed apps → jobalerts → Uninstall.
