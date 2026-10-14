# Packaging (v0.1.1)

All three packages repackage the official release `.deb`, so every channel ships
the same CI-tested binaries.

| Channel | File | Status |
|---|---|---|
| AUR | [`aur/PKGBUILD`](aur/PKGBUILD) | Ready to push to `aur.archlinux.org/ai-desktop-spike-bin.git` |
| Flathub | [`flatpak/com.aidesktop.spike.yml`](flatpak/com.aidesktop.spike.yml) + metainfo | **Not submittable as-is**: see below. Usable to build a local/self-hosted `.flatpak` |
| Snap | [`snap/snapcraft.yaml`](snap/snapcraft.yaml) | Ready for `snapcraft` + `snapcraft upload` |

After a release is published, fill the checksums from its `SHA256SUMS`
(the script re-downloads the `.deb` and checks it before writing anything):

```bash
packaging/update-checksums.sh 0.1.1
```

## Flathub: what a submission needs

From the [Flathub requirements](https://docs.flathub.org/docs/for-app-authors/requirements):

- **No AI-generated manifests, and no AI-opened PRs.** The Flathub manifest,
  PR text and replies must be written by the maintainer. The manifest here was
  AI-assisted, so it is a reference only. AI-assisted app code must be
  disclosed in the submission.
- **App ID:** `com.aidesktop.spike` needs control of `aidesktop.com`. For
  this repo the ID must be `io.github.anantsaksena.ai_desktop_spike`. Changing
  it also changes the data directory, so it needs a migration.
- **Development history:** projects that have existed only briefly are
  generally not accepted. Submit after a few tagged releases and real users.

## Permissions, and why

- **Network**: used only for the one-time, user-initiated model download
  (pinned URL, SHA-256 verified). Documents and questions never leave the machine.
- **Localhost listening** (Snap `network-bind`): the UI talks to its own backend
  and llama-server on `127.0.0.1`.
- **Keychain** (`org.freedesktop.secrets` / `password-manager-service`): stores
  the database key. Without it the app falls back to a `0600` key file.
- **No home-directory access** in Flatpak: PDFs come in through the file
  chooser or drag-and-drop.
