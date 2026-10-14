# Packaging (v0.1.1)

All three packages repackage the official release `.deb`, so every channel ships
the same CI-tested binaries.

| Channel | File | Status |
|---|---|---|
| AUR | [`aur/PKGBUILD`](aur/PKGBUILD) | Ready to push to `aur.archlinux.org/ai-desktop-spike-bin.git` |
| Flathub | [`flatpak/com.aidesktop.spike.yml`](flatpak/com.aidesktop.spike.yml) + metainfo | Ready for a PR to `flathub/flathub` |
| Snap | [`snap/snapcraft.yaml`](snap/snapcraft.yaml) | Ready for `snapcraft` + `snapcraft upload` |

After a release is published, fill the checksums from its `SHA256SUMS`
(the script re-downloads the `.deb` and checks it before writing anything):

```bash
packaging/update-checksums.sh 0.1.1
```

## Permissions, and why

- **Network**: used only for the one-time, user-initiated model download
  (pinned URL, SHA-256 verified). Documents and questions never leave the machine.
- **Localhost listening** (Snap `network-bind`): the UI talks to its own backend
  and llama-server on `127.0.0.1`.
- **Keychain** (`org.freedesktop.secrets` / `password-manager-service`): stores
  the database key. Without it the app falls back to a `0600` key file.
- **No home-directory access** in Flatpak: PDFs come in through the file
  chooser or drag-and-drop.
