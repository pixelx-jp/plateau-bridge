#!/usr/bin/env bash
# Run `plateau verify` on every bundled city's output directory.
#
# Expects the convention: each city's parquet lives under ./out_<short_name>/
# (or ./out_<code>/) — the same layout produced by the README's quickstart.

set -euo pipefail

declare -A CITIES=(
  ["渋谷区 13113"]="out_shibuya"
  ["新宿区 13104"]="out_shinjuku"
  ["横浜市 14100"]="out_yokohama"
  ["鎌倉市 14204"]="out_kamakura"
  ["名古屋市 23100"]="out_nagoya"
  ["大阪市 27100"]="out_osaka"
  ["福岡市 40130"]="out_fukuoka"
  ["札幌市 01100"]="out_sapporo"
)

# Use insertion order so the report is readable.
for label in "渋谷区 13113" "新宿区 13104" "横浜市 14100" "鎌倉市 14204" \
             "名古屋市 23100" "大阪市 27100" "福岡市 40130" "札幌市 01100"; do
  dir="${CITIES[$label]}"
  if [ ! -f "${dir}/buildings.parquet" ]; then
    printf "  [SKIP] %s — %s/buildings.parquet not present\n" "${label}" "${dir}"
    continue
  fi
  printf "==> %s (%s)\n" "${label}" "${dir}"
  plateau verify "${dir}" || true
  echo
done
