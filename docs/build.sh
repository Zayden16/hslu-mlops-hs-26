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

build_tex() {
    # Hand-written LaTeX (the proposal): two xelatex passes for hyperref,
    # auxiliary files kept out of docs/.
    local doc=$1 aux
    aux=$(mktemp -d)
    echo "pdf: $doc.tex -> $doc.pdf"
    local epoch
    epoch=$(git log -1 --format=%ct -- "$doc.tex" 2>/dev/null) || true
    [[ -n ${epoch:-} ]] || epoch=$(date +%s)
    for _ in 1 2; do
        SOURCE_DATE_EPOCH="$epoch" FORCE_SOURCE_DATE=1 \
            xelatex -interaction=nonstopmode -halt-on-error \
            -output-directory="$aux" "$doc.tex" >/dev/null \
            || { grep -A5 '^!' "$aux/$doc.log" >&2; return 1; }
    done
    mv "$aux/$doc.pdf" "$doc.pdf"
    rm -rf "$aux"
}

build_pdf() {
    local doc=$1
    # template.tex is the pandoc template, not a document
    if [[ -f "$doc.tex" && $doc != template ]]; then build_tex "$doc"; return; fi
    [[ -f "$doc.md" ]] || { echo "no such document: $doc.md or $doc.tex" >&2; return 1; }
    echo "pdf: $doc.md -> $doc.pdf"
    # Pin the embedded timestamp to the source's last commit, so rebuilding an
    # unchanged document does not churn the CreationDate. The output is still
    # not byte-identical across runs: xelatex picks a random font-subset tag
    # (e.g. SWFAZV+LMRoman10) each time. The rendered pages are identical, so
    # only re-commit a PDF when its Markdown actually changed.
    local epoch
    epoch=$(git log -1 --format=%ct -- "$doc.md" 2>/dev/null) || true
    [[ -n ${epoch:-} ]] || epoch=$(date +%s)
    SOURCE_DATE_EPOCH="$epoch" FORCE_SOURCE_DATE=1 \
        pandoc "$doc.md" -o "$doc.pdf" --template=template.tex --pdf-engine=xelatex
}

for mmd in ./*.mmd; do
    [[ -e $mmd ]] && render_diagram "$mmd"
done

if [[ $# -gt 0 ]]; then
    for doc in "$@"; do build_pdf "${doc%.md}"; done
else
    for src in ./*.md ./*.tex; do
        [[ -e $src ]] || continue
        name=$(basename "${src%.*}")
        [[ $name == template ]] || build_pdf "$name"
    done
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
