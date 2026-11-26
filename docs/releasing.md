# Releasing the desktop app

```sh
git tag v1.2.3 && git push origin v1.2.3
```

`.github/workflows/release.yml` then, for Windows:

1. sets the version from the tag (`src/jobalerts/__init__.py`);
2. builds the app with PyInstaller (`packaging/jobalerts.spec`);
3. runs the built app with `--self-test` (starts it in a temporary folder and checks the web UI);
4. packages it as an Inno Setup installer (`packaging/jobalerts.iss`);
5. makes a **draft** GitHub release with the files and `SHA256SUMS.txt`.

The maintainer then signs `SHA256SUMS.txt` with the release key (`tools/sign_release.py`), adds
`SHA256SUMS.txt.sig` and publishes the release. Installed apps don't see a draft.

A tag with a suffix (`v1.2.3-rc1`) makes a pre-release; installed apps don't offer pre-releases as
updates. An app on `1.2.3-rc1` is offered `1.2.3`. Actions → Release → Run workflow builds and tests without releasing (the files are on the
run's page).

Installed apps check `releases/latest` daily and tell an admin about a newer version. On
**Update now** they install it only if `SHA256SUMS.txt.sig` is a valid signature of `SHA256SUMS.txt`
with the release key (`RELEASE_KEY` in `src/jobalerts/updater.py`) and the installer matches
`SHA256SUMS.txt`. The repository must be public for them to download it.

The installer has no Windows code signature. Windows SmartScreen warns on first install;
[install.md](install.md) shows the steps past the warning.

Build locally (on the target system):

```sh
pip install ".[desktop]" pyinstaller
pyinstaller --noconfirm packaging/jobalerts.spec
dist/jobalerts/jobalerts --self-test
```
