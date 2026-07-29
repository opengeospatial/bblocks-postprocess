#!/usr/bin/env bash
# Create the next v1.<minor>.<patch> git tag based on the highest existing tag.
#
# Usage: scripts/tag-release.sh [major|minor|patch] [--push]
#   major/minor/patch defaults to "patch"
#   --push also pushes the new tag to origin

set -euo pipefail

bump="${1:-patch}"
push=false
for arg in "$@"; do
  if [[ "$arg" == "--push" ]]; then
    push=true
  fi
done

if [[ "$bump" != "major" && "$bump" != "minor" && "$bump" != "patch" ]]; then
  echo "Usage: $0 [major|minor|patch] [--push]" >&2
  exit 1
fi

latest=$(git tag --list 'v1.*.*' | sort -V | tail -1)

if [[ -z "$latest" ]]; then
  major=1
  minor=0
  patch=0
else
  version="${latest#v}"
  IFS='.' read -r major minor patch <<< "$version"
fi

case "$bump" in
  major)
    major=$((major + 1))
    minor=0
    patch=0
    ;;
  minor)
    minor=$((minor + 1))
    patch=0
    ;;
  patch)
    patch=$((patch + 1))
    ;;
esac

new_tag="v${major}.${minor}.${patch}"

echo "Latest tag: ${latest:-<none>}"
echo "New tag:    ${new_tag}"

git tag "$new_tag"

if [[ "$push" == true ]]; then
  git push origin "$new_tag"
else
  echo "Tag created locally. Push it with: git push origin ${new_tag}"
fi