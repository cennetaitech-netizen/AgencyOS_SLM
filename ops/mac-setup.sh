#!/usr/bin/env bash
# Install everything the AgencyOS Assistant test site needs on a Mac, once. Safe to run again: it skips what's
# already installed. Guide: docs/TEST_SITE.md ("On your own computer").
#
#   ops/mac-setup.sh               install everything, including Ollama and one local model
#   ops/mac-setup.sh --no-ollama   skip Ollama (use only Gemini or another hosted model)
#
# Installs:
#   - Xcode Command Line Tools: git and python3 (Apple's installer opens in a window)
#   - Homebrew: installs the rest (asks for your Mac password)
#   - Docker Desktop: runs the site's database (Postgres), Redis, the API and background jobs
#   - cloudflared: the public https address for testers elsewhere (ops/test-site.sh --public)
#   - Ollama and one local model: free answers on this Mac, no API key (qwen2.5:7b with 16 GB of memory or
#     more, else qwen2.5:3b)
# Not needed on the Mac: Postgres, Redis, Node and Python packages. They run inside Docker.
# Gemini needs no install, only its API key in .env.testsite (ops/test-site.sh creates the file).
set -euo pipefail

OLLAMA=1
for arg in "$@"; do
  case "$arg" in
    --no-ollama) OLLAMA=0 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

if [[ $(uname -s) != Darwin ]]; then
  echo "This script is for macOS. On Windows or Linux, install Docker Desktop (docker.com) and git." >&2
  exit 1
fi

# 1. Xcode Command Line Tools (git, python3)
if ! xcode-select -p >/dev/null 2>&1; then
  say "Installing Apple's Command Line Tools (git, python3). Click Install in the window that opens."
  xcode-select --install || true
  echo "When that install has finished, run this script again: ops/mac-setup.sh"
  exit 0
fi
say "Command Line Tools: installed"

# 2. Homebrew
if ! have brew; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ -x $b ]] && eval "$("$b" shellenv)" && break
  done
fi
if ! have brew; then
  say "Installing Homebrew (it asks for your Mac password)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ -x $b ]] && eval "$("$b" shellenv)" && break
  done
  have brew || { echo "Homebrew didn't install; see the messages above." >&2; exit 1; }
fi
# Make brew available in new Terminal windows too.
BREW_BIN=$(command -v brew)
if ! grep -qs "brew shellenv" "$HOME/.zprofile"; then
  echo "eval \"\$($BREW_BIN shellenv)\"" >> "$HOME/.zprofile"
fi
say "Homebrew: $(brew --version | head -1)"

# 3. Docker Desktop
if [[ ! -d /Applications/Docker.app ]] && ! have docker; then
  say "Installing Docker Desktop"
  brew install --cask docker-desktop || brew install --cask docker
fi
if ! docker info >/dev/null 2>&1; then
  say "Starting Docker Desktop. The first time, accept its terms in the window (no account needed)."
  open -a Docker
  for _ in $(seq 1 90); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi
if docker info >/dev/null 2>&1; then
  say "Docker: running ($(docker --version))"
else
  say "Docker is installed but not running yet. Finish its first-start window, then run this script again."
  exit 1
fi

# 4. cloudflared (public address for testers)
have cloudflared || { say "Installing cloudflared"; brew install cloudflared; }
say "cloudflared: $(cloudflared --version 2>&1 | head -1)"

# 5. Ollama and a local model
if [[ $OLLAMA == 1 ]]; then
  have ollama || { say "Installing Ollama"; brew install ollama; }
  if ! curl -sf --max-time 3 http://localhost:11434/api/tags >/dev/null; then
    say "Starting Ollama (it starts again by itself after a restart)"
    brew services start ollama
    for _ in $(seq 1 30); do
      curl -sf --max-time 3 http://localhost:11434/api/tags >/dev/null && break
      sleep 1
    done
  fi
  MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  if (( MEM_GB >= 16 )); then MODEL=qwen2.5:7b; else MODEL=qwen2.5:3b; fi
  if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
    say "Ollama model $MODEL: already downloaded"
  else
    say "Downloading the local model $MODEL for ${MEM_GB} GB of memory (a few GB; takes a while)"
    ollama pull "$MODEL"
  fi
  say "Ollama: $(ollama --version 2>&1 | tail -1)"
fi

say "Everything is installed:"
echo "  git        $(git --version | awk '{print $3}')"
echo "  python3    $(python3 --version 2>&1 | awk '{print $2}')"
echo "  Docker     $(docker --version | awk '{print $3}' | tr -d ,)"
echo "  cloudflared $(cloudflared --version 2>&1 | awk '{print $3}' | head -1)"
[[ $OLLAMA == 1 ]] && echo "  Ollama     $(ollama list 2>/dev/null | awk 'NR>1 {print $1}' | paste -sd, -)"
cat <<'EOF'

Next, from this folder:
  ops/test-site.sh            start the site (the first build takes a few minutes); it prints your staff link
Then for Gemini: open .env.testsite, put your key after CHAT_GEMINI_API_KEY=, and run ops/test-site.sh again.
EOF
