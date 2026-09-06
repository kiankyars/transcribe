#!/bin/bash
set -euo pipefail

# Uninstall the simple iCloud-inbox LaunchAgent and its plist.
# Safe to run even if the agent is not installed.

label="com.siri.simple"
UID_VALUE="$(id -u)"
plist="$HOME/Library/LaunchAgents/${label}.plist"

echo "Unloading $label..."
launchctl bootout "gui/${UID_VALUE}" "$plist" >/dev/null 2>&1 || true
launchctl bootout "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true

rm -f "$plist"

echo "Removed $label (if it was present)."
echo "plist: $plist (deleted)"
