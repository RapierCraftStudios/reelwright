#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source this to load Reelwright secrets into the shell: `. scripts/secrets.sh`
# Secrets live OUTSIDE the repo (default ~/.config/reelwright/secrets.env, mode 600) so they can never be committed.
_rw_secrets="${REELWRIGHT_SECRETS:-$HOME/.config/reelwright/secrets.env}"
if [ -r "$_rw_secrets" ]; then set -a; . "$_rw_secrets"; set +a; else echo "reelwright: no secrets file at $_rw_secrets (mocks will be used)" >&2; fi
unset _rw_secrets
