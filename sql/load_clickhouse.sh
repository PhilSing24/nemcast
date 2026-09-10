#!/usr/bin/env bash
#
# Load the NEMCast warehouse parquet files into local ClickHouse.
#
#     bash sql/load_clickhouse.sh
#
# ClickHouse reads parquet natively, including timestamp logical types, so no
# conversion is needed. Tables are created from the first file's inferred schema
# rather than hand-written DDL — with 22 to 30 columns per table and mixed case
# (SETTLEMENTDATE uppercase, known_at lowercase), hand-writing invites typos.
#
# Three details that took a couple of attempts:
#
#   TSVRaw, not TSV, for the DESCRIBE output — TSV escapes the quotes inside
#   DateTime64(9, 'UTC') and the resulting DDL will not parse.
#
#   allow_nullable_key = 1, because parquet inference marks every column
#   Nullable and ClickHouse otherwise refuses them in a sort key. None of the
#   key columns actually contain nulls.
#
#   Files are piped in rather than read with file(), which only reads from
#   ClickHouse's user_files directory. Piping works from any path.
#
# The client connects over ::1: a Python process holds IPv4 port 9000, leaving
# ClickHouse bound to IPv6 only.

set -euo pipefail

BIN="${HOME}/clickhouse"
CH="${BIN} client --host ::1"
WAREHOUSE="${HOME}/nemcast/data/warehouse"
DB="nemcast"

# table -> the timestamp column it partitions on
declare -A TIME_COL=(
  [dispatchprice]=SETTLEMENTDATE
  [dispatchregionsum]=SETTLEMENTDATE
  [predispatchprice]=DATETIME
  [predispatchregionsum]=DATETIME
)

# table -> ORDER BY. Region first, then time, then vintage where it exists, so
# every forecast of one interval sits physically adjacent.
declare -A ORDER_BY=(
  [dispatchprice]="REGIONID, SETTLEMENTDATE, INTERVENTION, source"
  [dispatchregionsum]="REGIONID, SETTLEMENTDATE, INTERVENTION, source"
  [predispatchprice]="REGIONID, DATETIME, vintage, INTERVENTION, source"
  [predispatchregionsum]="REGIONID, DATETIME, vintage, INTERVENTION, source"
)

echo "clickhouse $(${CH} --query 'SELECT version()')"
${CH} --query "CREATE DATABASE IF NOT EXISTS ${DB}"

for table in dispatchprice dispatchregionsum predispatchprice predispatchregionsum; do
  dir="${WAREHOUSE}/${table}"
  if [[ ! -d "$dir" ]]; then
    echo "  ${table}: no directory, skipping"
    continue
  fi

  shopt -s nullglob
  files=("$dir"/*.parquet)
  shopt -u nullglob
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "  ${table}: no files, skipping"
    continue
  fi

  echo
  echo "=== ${table} (${#files[@]} files)"

  # clickhouse-local has no path restriction, so it infers the schema straight
  # from the warehouse.
  schema=$("${BIN}" local --query \
      "DESCRIBE TABLE file('${files[0]}', Parquet) FORMAT TSVRaw" \
      | awk -F'\t' '{printf "  `%s` %s,\n", $1, $2}' | sed '$ s/,$//')

  ${CH} --query "DROP TABLE IF EXISTS ${DB}.${table}"
  ${CH} --query "
    CREATE TABLE ${DB}.${table}
    (
${schema}
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMM(${TIME_COL[$table]})
    ORDER BY (${ORDER_BY[$table]})
    SETTINGS index_granularity = 8192, allow_nullable_key = 1
  "

  for f in "${files[@]}"; do
    printf '    %-32s ' "$(basename "$f")"
    ${CH} --query "INSERT INTO ${DB}.${table} FORMAT Parquet" < "$f"
    printf 'total %s\n' "$(${CH} --query "SELECT count() FROM ${DB}.${table}")"
  done
done

echo
echo "=== loaded"
${CH} --query "
  SELECT
      table,
      formatReadableQuantity(sum(rows))      AS rows,
      formatReadableSize(sum(bytes_on_disk)) AS on_disk,
      count()                                AS parts
  FROM system.parts
  WHERE database = '${DB}' AND active
  GROUP BY table
  ORDER BY table
  FORMAT PrettyCompact
"

echo
echo "=== sanity"
${CH} --query "
  SELECT
      min(SETTLEMENTDATE)     AS first_interval,
      max(SETTLEMENTDATE)     AS last_interval,
      round(min(RRP), 2)      AS min_rrp,
      round(max(RRP), 2)      AS max_rrp,
      countDistinct(REGIONID) AS regions
  FROM ${DB}.dispatchprice
  FORMAT PrettyCompact
"

${CH} --query "
  SELECT source, count() AS n
  FROM ${DB}.dispatchprice
  GROUP BY source
  ORDER BY n DESC
  FORMAT PrettyCompact
"
