# Releasing the desktop app

```sh
git tag v1.2.3 && git push origin v1.2.3
```

`.github/workflows/release.yml` then, for Windows, Mac (Apple silicon) and Mac (Intel):

1. sets the version from the tag (`src/jobalerts/__init__.py`);
2. builds the app with PyInstaller (`packaging/jobalerts.spec`);
3. runs the built app with `--self-test` (starts it in a temporary folder and checks the web UI);
4. packages it: Inno Setup installer (`packaging/jobalerts.iss`) or `.dmg`;
5. publishes a GitHub release with the files and `SHA256SUMS.txt`.

A failed Intel Mac build doesn't stop the release. A tag with a suffix (`v1.2.3-rc1`) makes a
pre-release; installed apps don't offer pre-releases as updates. Actions → Release → Run workflow builds
and tests without releasing (the files are on the run's page).

Installed apps check `releases/latest` daily and install a newer version only if its file matches
`SHA256SUMS.txt`. The repository must be public for them to download it.

The apps aren't signed. Windows SmartScreen and macOS Gatekeeper warn on first install;
[install.md](install.md) shows the steps past the warning.

Build locally (on the target system):

```sh
pip install ".[desktop]" pyinstaller
pyinstaller --noconfirm packaging/jobalerts.spec
dist/jobalerts/jobalerts --self-test        # Mac: dist/jobalerts.app/Contents/MacOS/jobalerts --self-test
```
