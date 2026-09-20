#!/usr/bin/env python3
"""Compare the catalog with a pinned Danbooru CSV; never treat absent tags as invalid.

Python 3.10+, standard library only. CSV aliases may be stale, so they are
suggestions only. This script never downloads or executes third-party code.
"""
import argparse
import copy
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def normalize(tag):
    # Match index.html's existing lookup/output spelling, not fuzzy matching.
    return re.sub(r"_+", "_", re.sub(r"\s+", "_", tag.strip().lower())).strip("_")


def read_reference(path):
    tags, aliases = {}, defaultdict(set)
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) < 4:
                raise ValueError("Reference requires name, category, post_count, aliases columns")
            name, category, count = row[0], int(row[1]), int(row[2])
            if not name or name != normalize(name) or category not in (0, 1, 3, 4, 5) or count < 0:
                raise ValueError(f"Invalid reference row: {row[:3]!r}")
            if name in tags:
                raise ValueError(f"Duplicate reference name: {name}")
            tags[name] = {"category": category, "post_count": count}
            # Only column 4 is aliases; column 5 may contain a translation.
            for alias in row[3].split(","):
                if alias:
                    aliases[alias].add(name)
    if not tags:
        raise ValueError("Empty reference")
    return tags, aliases


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError("Catalog must be a nonempty group-to-rows object")
    for group, rows in catalog.items():
        if not isinstance(rows, list):
            raise ValueError(f"Invalid group: {group}")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("tags"), list):
                raise ValueError(f"Invalid row in {group}")
            if not row["tags"] or any(not isinstance(t, str) or not t.strip() for t in row["tags"]):
                raise ValueError(f"Invalid tags in {group}")


def inventory(catalog):
    rows = [row for group in catalog.values() for row in group]
    tags = [tag for row in rows for tag in row["tags"]]
    return {
        "groups": len(catalog), "rows": len(rows), "tag_occurrences": len(tags),
        "unique_raw_tags": len(set(tags)),
        "unique_normalized_tags": len({normalize(t) for t in tags}),
        "uppercase_occurrences": sum(t != t.lower() for t in tags),
        "whitespace_occurrences": sum(bool(re.search(r"\s", t)) for t in tags),
        "placeholder_occurrences": sum(bool(re.search(r"[*{}=]", t)) for t in tags),
    }


def audit(catalog, reference, aliases):
    contexts, variants = defaultdict(set), defaultdict(set)
    for group, rows in catalog.items():
        for row in rows:
            for raw in row["tags"]:
                tag = normalize(raw)
                variants[tag].add(raw)
                contexts[tag].add(f"{group} / {row.get('category', '')} / {row.get('subCategory', '')} / {row.get('name', '')}")
    result = []
    for tag in sorted(variants):
        if tag in reference:
            status = "snapshot_match"
        elif re.search(r"[*{}=]", tag):
            status = "placeholder_review"
        elif tag in aliases:
            status = "alias_candidate_unverified"
        else:
            status = "not_in_snapshot"
        meta = reference.get(tag, {})
        result.append({
            "tag": tag, "status": status, "category": meta.get("category", ""),
            "post_count": meta.get("post_count", ""),
            "alias_candidates": json.dumps(sorted(aliases.get(tag, [])), ensure_ascii=False),
            "raw_variants": json.dumps(sorted(variants[tag]), ensure_ascii=False),
            "locations": json.dumps(sorted(contexts[tag]), ensure_ascii=False),
        })
    return result


def refresh(catalog, reference, aliases, additions):
    updated = copy.deepcopy(catalog)
    changes = []
    for group, rows in updated.items():
        for row in rows:
            result = []
            for raw in row["tags"]:
                normalized = normalize(raw)
                target = normalized if normalized in reference else raw
                if target != raw:
                    changes.append({"type": "spelling", "group": group, "name": row["name"], "from": raw, "to": target})
                # Deduplicate only confirmed tags in the SAME row. Keep contexts.
                if target in reference and target in result:
                    changes.append({"type": "duplicate_in_row", "group": group, "name": row["name"], "from": raw, "to": target})
                else:
                    result.append(target)
            row["tags"] = result
    represented = {normalize(t) for rows in updated.values() for row in rows for t in row["tags"]}
    # Avoid adding a second entry even if the old spelling is an unverified alias.
    represented |= {target for tag in list(represented) for target in aliases.get(tag, [])}
    for entry in additions:
        group, row = entry["group"], entry["row"]
        validate_catalog({group: [row]})
        if group not in updated or len(row["tags"]) != 1:
            raise ValueError("Additions require an existing group and exactly one tag")
        tag = row["tags"][0]
        if tag not in reference or reference[tag]["category"] != 0:
            raise ValueError(f"Addition is not a confirmed general tag: {tag}")
        if tag in represented:
            continue
        updated[group].append(copy.deepcopy(row))
        represented.add(tag)
        changes.append({"type": "addition", "group": group, "name": row["name"], "to": tag})
    return updated, changes


def write_json(path, value):
    # Atomic file replacement; no half-written catalog on interruption.
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("tags.json"))
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--source-url", required=True, help="Pinned source commit/file URL")
    parser.add_argument("--source-date", required=True, help="Snapshot date, not today's date")
    parser.add_argument("--additions", type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--apply", action="store_true", help="Apply confirmed spelling and reviewed additions only")
    args = parser.parse_args()
    catalog_bytes = args.catalog.read_bytes()
    catalog = json.loads(catalog_bytes)
    validate_catalog(catalog)
    reference, aliases = read_reference(args.reference)
    additions = json.loads(args.additions.read_text(encoding="utf-8")) if args.additions else []
    records = audit(catalog, reference, aliases)
    updated, changes = refresh(catalog, reference, aliases, additions)
    validate_catalog(updated)
    summary = {
        "source": {
            "url": args.source_url, "snapshot_date": args.source_date,
            "sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
            "reference_tags": len(reference), "kind": "third_party_snapshot",
            "live_danbooru_verified": False,
            "limitations": ["Snapshot may omit low-count or deprecated tags.",
                           "Aliases may include historical mappings; never auto-replaced.",
                           "Not found does NOT mean invalid; model-specific prompts are preserved."],
        },
        "input_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "before": inventory(catalog), "after": inventory(updated),
        "comparison": dict(Counter(r["status"] for r in records)),
        "changes": dict(Counter(c["type"] for c in changes)),
        "applied": args.apply,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "tag-audit.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
    write_json(args.output / "tag-audit-summary.json", summary)
    write_json(args.output / "tag-changes.json", changes)
    if args.apply:
        # Preserve original compact storage to avoid a format-only megabyte diff.
        temporary = args.catalog.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(updated, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(args.catalog)
    else:
        write_json(args.output / "tags.proposed.json", updated)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
