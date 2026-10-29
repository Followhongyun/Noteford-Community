#!/bin/sh
# macOS / non-PowerShell entry. Delegates to noteford.py next to this file.
set -eu
dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
exec python3 "$dir/noteford.py" "$@"
