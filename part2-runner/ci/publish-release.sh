#!/usr/bin/env bash
# Publishes the current runner image (after smoke-test.sh promoted it) as a GitHub Release:
#   tag ami-YYYYMMDD-HHMM; notes = what changed since the previous image, then IMAGE.md; assets
#   manifest.json and IMAGE.md. spellbound3d can read the latest with:
#   gh release view --repo stickleprojects/aws-build-runner
# Needs aws (SSM read), gh with contents:write, jq, python3.
#
#   part2-runner/ci/publish-release.sh
set -Eeuo pipefail
cd "$(dirname "$0")/../ami"
export AWS_REGION=ap-southeast-2
REPO="${GITHUB_REPOSITORY:-stickleprojects/aws-build-runner}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
ami="$(aws ssm get-parameter --name /build/runner/ami-id --query Parameter.Value --output text)"
aws ssm get-parameter --name /build/runner/manifest --query Parameter.Value --output text | jq . > "$work/manifest.json"
python3 -I make-image-md.py "$work/manifest.json" "$work/IMAGE.md" >/dev/null
built="$(jq -r .built "$work/manifest.json")"
tag="ami-$(date -u -d "$built" +%Y%m%d-%H%M)"

if gh release view "$tag" --repo "$REPO" >/dev/null 2>&1; then
  echo "release ${tag} already exists"; exit 0
fi

# What changed since the previous release (its manifest.json asset).
changes="First published image."
if prev="$(gh release view --repo "$REPO" --json tagName --jq .tagName 2>/dev/null)" && [[ -n "$prev" ]]; then
  if gh release download "$prev" --repo "$REPO" --pattern manifest.json --dir "$work/prev" >/dev/null 2>&1; then
    changes="$(jq -r -n --slurpfile old "$work/prev/manifest.json" --slurpfile new "$work/manifest.json" '
      ($old[0].tools + {runner: $old[0].runner.version, spellbound3d: $old[0].spellbound3d_commit[0:12], os: $old[0].os}) as $o |
      ($new[0].tools + {runner: $new[0].runner.version, spellbound3d: $new[0].spellbound3d_commit[0:12], os: $new[0].os}) as $n |
      [$n | keys[] | select($o[.] != $n[.]) | "- **\(.)**: \($o[.] // "none") → \($n[.])"] |
      if length == 0 then "No version changes since the previous image (security updates from Ubuntu only)." else join("\n") end')"
    changes="Since \`${prev}\`:"$'\n\n'"${changes}"
  fi
fi
{
  echo "AMI \`${ami}\` (Sydney), current runner image since this release."
  echo
  echo "## What changed"
  echo
  echo "$changes"
  echo
  sed '1d' "$work/IMAGE.md"   # IMAGE.md without its own title
} > "$work/notes.md"

gh release create "$tag" --repo "$REPO" --title "Runner image ${tag#ami-}" --notes-file "$work/notes.md" --latest \
  "$work/manifest.json" "$work/IMAGE.md"
echo "published release ${tag}"
