#!/usr/bin/env bash
# Rebuild the milestone PDFs from Markdown.
#
#   ./docs/build.sh              # all documents
#   ./docs/build.sh proposal     # just one
#
# Requires: pandoc, xelatex (MacTeX/TeX Live), npx (for the mermaid diagram).
set -euo pipefail
cd "$(dirname "$0")"

render_diagram() {
    local src=$1 out=${1%.mmd}.png
    if [[ ! -f $out || $src -nt $out ]]; then
        echo "diagram: $src -> $out"
        npx -y @mermaid-js/mermaid-cli@11 -i "$src" -o "$out" -b white -s 3 >/dev/null
    fi
}

build_pdf() {
    local doc=$1
    [[ -f "$doc.md" ]] || { echo "no such document: $doc.md" >&2; return 1; }
    echo "pdf: $doc.md -> $doc.pdf"
    pandoc "$doc.md" -o "$doc.pdf" --template=template.tex --pdf-engine=xelatex
}

for mmd in ./*.mmd; do
    [[ -e $mmd ]] && render_diagram "$mmd"
done

if [[ $# -gt 0 ]]; then
    for doc in "$@"; do build_pdf "${doc%.md}"; done
else
    for md in ./*.md; do build_pdf "$(basename "${md%.md}")"; done
fi

for pdf in ./*.pdf; do
    [[ -e $pdf ]] || continue
    pages=$(pdfinfo "$pdf" 2>/dev/null | awk '/^Pages:/{print $2}')
    # Every milestone document in this module is capped at two pages.
    if [[ -n ${pages:-} && $pages -gt 2 ]]; then
        echo "WARNING: $pdf is $pages pages, the limit is 2" >&2
    else
        echo "ok: $pdf ($pages pages)"
    fi
done
