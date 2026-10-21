#!/usr/bin/env bash
# Create the next v1.<minor>.<patch> git tag based on the highest existing tag.
#
# Usage: scripts/tag-release.sh [major|minor|patch] [--push] [-m NOTES | --notes-file FILE] [-e]
#   major/minor/patch defaults to "patch"
#   --push             also pushes the new tag to origin
#   -m, --notes NOTES  release notes (e.g. a Markdown bullet list); stored as the
#                      annotated tag's message, and build-docker.yml puts them on
#                      the GitHub release page above the auto-generated changelog
#   --notes-file FILE  same, reading the notes from FILE ("-" for stdin)
#   -e, --edit         open $EDITOR (git's usual tag editor) to write the notes;
#                      pre-filled with -m/--notes-file if also given. Aborting with
#                      an empty message cancels the release
#
# Without notes, a lightweight tag is created and the release page only gets the
# auto-generated changelog.

set -euo pipefail

usage() {
  echo "Usage: $0 [major|minor|patch] [--push] [-m NOTES | --notes-file FILE] [-e]" >&2
  exit 1
}

bump="patch"
push=false
notes=""
edit=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    major|minor|patch)
      bump="$1"
      ;;
    --push)
      push=true
      ;;
    -e|--edit)
      edit=true
      ;;
    -m|--notes)
      [[ $# -ge 2 ]] || usage
      notes="$2"
      shift
      ;;
    --notes-file)
      [[ $# -ge 2 ]] || usage
      if [[ "$2" == "-" ]]; then
        notes=$(cat)
      else
        notes=$(cat "$2")
      fi
      shift
      ;;
    *)
      usage
      ;;
  esac
  shift
done

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

if [[ -n "$notes" || "$edit" == true ]]; then
  tag_args=(-a)
  if [[ -n "$notes" ]]; then
    tag_args+=(-m "$notes")
  fi
  if [[ "$edit" == true ]]; then
    tag_args+=(-e)
  fi
  git tag "${tag_args[@]}" "$new_tag"
  echo "Release notes:"
  git tag -l --format='%(contents)' "$new_tag"
else
  git tag "$new_tag"
fi

if [[ "$push" == true ]]; then
  git push origin "$new_tag"
else
  echo "Tag created locally. Push it with: git push origin ${new_tag}"
fi