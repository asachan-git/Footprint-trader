#!/usr/bin/env bash
# Pre-commit hook: any change to architecture-critical paths MUST also update README.md.
#
# Install:
#   ln -s ../../scripts/check_readme_synced.sh .git/hooks/pre-commit
# Or via pre-commit framework: add to .pre-commit-config.yaml as a local hook.

set -euo pipefail

# Paths that, if touched, require README.md to also be in the staged diff.
WATCH=(
  "pipeline/"
  "llm/"
  "execution/"
  "server/"
  "prompts/builder.py"
  "config/settings.yaml"
)

staged="$(git diff --cached --name-only)"
[ -z "$staged" ] && exit 0

needs_readme=0
for path in "${WATCH[@]}"; do
  if grep -qE "^${path}" <<<"$staged"; then
    needs_readme=1
    break
  fi
done

[ "$needs_readme" -eq 0 ] && exit 0

if grep -qE "^README\.md$" <<<"$staged"; then
  exit 0
fi

cat <<EOF >&2
Pre-commit BLOCKED: architecture-critical path(s) changed without README.md update.

Staged paths matching watchlist:
$(grep -E "^($(IFS='|'; echo "${WATCH[*]}"))" <<<"$staged" | sed 's/^/  - /')

README.md is the living architecture doc. Update it in the same commit, then retry.

To bypass for a true non-architectural change, edit scripts/check_readme_synced.sh
and narrow the WATCH list; do not skip the hook.
EOF
exit 1
