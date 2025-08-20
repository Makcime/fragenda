#!/usr/bin/env bash
set -euo pipefail

INPUT="$1"
BASENAME="$(basename "$INPUT" .pdf)"
OUTDIR="$BASENAME"

mkdir -p "$OUTDIR/pages"

# 1) Burst into pages
pdftk "$INPUT" burst output "$OUTDIR/pages/pg_%04d.pdf"

# 2) Loop through pages
for page in "$OUTDIR"/pages/pg_*.pdf; do
  # Try to get the first line; don't abort if pdftotext fails
  firstline="$(
    { pdftotext -layout -f 1 -l 1 "$page" - 2>/dev/null || true; } | head -n1
  )"

  # Fallback to page basename if empty
  [[ -z "$firstline" ]] && firstline="$(basename "$page" .pdf)"

  # Sanitize -> safe filename (strip/replace bad chars, collapse spaces)
  safe="$(printf '%s' "$firstline" \
      | sed -E 's/^[[:space:]]+|[[:space:]]+$//g; s/[\/:*?"<>|]+/_/g; s/[[:space:]]+/_/g')"
  # Avoid leading dot (hidden file)
  [[ "$safe" = .* ]] && safe="${safe#.}"

  new="$OUTDIR/${safe}.pdf"

  # If name exists, append page id to keep everything
  if [[ -e "$new" ]]; then
    basepg="$(basename "$page" .pdf)"   # e.g., pg_0007
    new="$OUTDIR/${safe}_${basepg}.pdf"
  fi

  mv "$page" "$new"
  echo "Created: $new"
done

# Optional: clean up pdftk metadata file
rm -f "$OUTDIR"/pages/doc_data.txt
# Leave the pages/ dir since it's empty now? Remove if empty:
rmdir "$OUTDIR/pages" 2>/dev/null || true
