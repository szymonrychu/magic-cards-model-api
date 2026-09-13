#!/bin/sh
# Check the deck-guides corpus (github.com/szymonrychu/mtg-decks) out into
# the seat Job's guides volume as a WRITABLE clone: a local `main` tracking
# `origin/main`, with full history, so the agent can commit a guide change,
# rebase it on `origin/main` and push a branch for a pull request.
#
# Baked into the agent image by Dockerfile.agent and run by the `guides`
# initContainer in deploy/seat-job.yaml. `tests/test_k8s_seats.py` runs this
# exact file against a local bare repo.
#
# - Partial, not shallow: `--filter=blob:none` fetches every commit and tree
#   but no file contents up front; the checkout fetches the blobs it needs
#   and later reads fetch the rest on demand. A `--depth` clone would leave
#   the agent unable to find a merge base with `origin/main`.
# - The token is read from a mounted file (GH_PAT_FILE), never an env var.
#   It sits in the remote URL only for the fetch and the checkout, and is
#   stripped back out before anything else runs; the script refuses to
#   finish if `x-access-token` is still in .git/config. From then on the
#   agent authenticates through the credential helper the wrapper writes to
#   its ~/.gitconfig (src/mtg_agent/wrapper/bootstrap.py), which reads the
#   same file - so a rotated Secret reaches a running pod without a restart.
# - GUIDES_REF reaches git, so it is still validated as a ref before
#   anything runs (a leading `-` is a git option, and `--upload-pack=<cmd>`
#   runs a command). Writable mode then requires it to be exactly `main`: a
#   pinned tag or sha has no branch for the agent to commit on.
set -eu

dir="${GUIDES_DIR:-/guides}"
ref="${GUIDES_REF:-main}"
token_file="${GH_PAT_FILE:-/var/run/mtg-decks-git/GH_PAT}"
repo="github.com/szymonrychu/mtg-decks.git"

printf %s "$ref" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/-]*$' || {
  echo "GUIDES_REF is not a ref: $ref" >&2
  exit 1
}
[ "$ref" = main ] || {
  echo "GUIDES_REF must be main: the guides checkout is writable and the agent commits on a local main tracking origin/main, got: $ref" >&2
  exit 1
}
[ -r "$token_file" ] || {
  echo "GH_PAT file is not readable: $token_file" >&2
  exit 1
}
token="$(cat "$token_file")"
[ -n "$token" ] || {
  echo "GH_PAT file is empty: $token_file" >&2
  exit 1
}

git config --global --add safe.directory "$dir"
find "$dir" -mindepth 1 -delete
git -c init.defaultBranch=main init -q "$dir"
git -C "$dir" remote add origin "https://x-access-token:${token}@${repo}"
git -C "$dir" fetch -q --filter=blob:none origin "+refs/heads/main:refs/remotes/origin/main"
git -C "$dir" checkout -q -B main --track origin/main
git -C "$dir" remote set-url origin "https://${repo}"
rm -f "$dir/.git/FETCH_HEAD"
if grep -q "x-access-token" "$dir/.git/config"; then
  echo "the token is still in $dir/.git/config" >&2
  exit 1
fi

git -C "$dir" rev-parse HEAD > "$dir/.guides-revision"
# The stamp lives in the working tree; keep it out of `git status` so no
# `git add -A` of the agent's ever commits it.
printf '/.guides-revision\n' >> "$dir/.git/info/exclude"
echo "guides main at $(cat "$dir/.guides-revision"), writable, tracking origin/main"
