#!/usr/bin/env bash
# GitHub Ubuntu runners can select an extremely slow Azure package mirror.
set -euo pipefail

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  mirror_file="${COURSE_CI_APT_MIRROR_FILE:-/etc/apt/apt-mirrors.txt}"
  if [[ -f "$mirror_file" ]]; then
    sudo sed -i.bak 's|http[s]*://azure.archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' "$mirror_file"
  fi
  sudo apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update
  sudo apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=2 -o Acquire::http::Timeout=30 install --no-install-recommends -y ffmpeg
fi
ffmpeg -version
ffprobe -version
