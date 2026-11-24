#!/usr/bin/env bash
# kipipe installer for Linux Mint / Ubuntu. Installs into ~/.local, no sudo
# needed unless system packages are missing (it tells you which).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${KIPIPE_HOME:-$HOME/.local/share/kipipe}"
APP="$HOME_DIR/app"
VENV="$HOME_DIR/venv"
BIN="$HOME/.local/bin"

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!!\033[0m %s\n' "$*"; }

missing=()
if command -v kicad-cli >/dev/null; then
  /usr/bin/python3 -c "import pcbnew" 2>/dev/null || missing+=("KiCad Python bindings (check /usr/bin/python3 -c 'import pcbnew')")
elif command -v flatpak >/dev/null && flatpak info org.kicad.KiCad >/dev/null 2>&1; then
  say "using Flatpak KiCad (org.kicad.KiCad)"
else
  missing+=("KiCad: flatpak install flathub org.kicad.KiCad   (or the apt PPA)")
fi
python3 -c "import venv, ensurepip" 2>/dev/null || missing+=("python3-venv (sudo apt install python3-venv)")
if command -v java >/dev/null; then
  jv=$(java -version 2>&1 | sed -n 's/.*version "\([0-9]*\).*/\1/p' | head -1)
  [ "${jv:-0}" -ge 21 ] || missing+=("Java 21+ (sudo apt install openjdk-21-jre), found $jv")
else
  missing+=("Java 21+ (sudo apt install openjdk-21-jre)")
fi
if [ ${#missing[@]} -gt 0 ]; then
  warn "missing dependencies:"
  for m in "${missing[@]}"; do echo "   - $m"; done
  exit 1
fi

say "installing app to $APP"
mkdir -p "$HOME_DIR" "$BIN"
rm -rf "$APP"
mkdir -p "$APP"
cp -r "$SRC/kipipe" "$SRC/examples" "$SRC/README.md" "$APP/"

say "creating virtualenv"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$SRC/requirements.txt"

cat > "$BIN/kipipe" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$APP\${PYTHONPATH:+:\$PYTHONPATH}"
# -P: don't put the current directory on sys.path (so a source checkout in \$PWD can't shadow the install)
exec "$VENV/bin/python" -P -m kipipe "\$@"
EOF
chmod +x "$BIN/kipipe"

say "fetching Freerouting for your Java version"
"$BIN/kipipe" fetch-router || warn "Freerouting download failed; it will be retried on first run"

mkdir -p "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$SRC/kipipe/static/icon.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/kipipe.svg" 2>/dev/null || true
cat > "$HOME/.local/share/applications/kipipe.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=kipipe
Comment=Text/image to routed KiCad board
Exec=$BIN/kipipe serve --open
Icon=kipipe
Terminal=true
Categories=Development;Electronics;
EOF

say "checking environment"
"$BIN/kipipe" doctor || true
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not on PATH; add it to ~/.bashrc";; esac
say "done. Start the UI with:  kipipe serve --open"
