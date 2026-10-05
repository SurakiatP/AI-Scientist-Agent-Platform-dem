#!/usr/bin/env python3
"""Export the sanitized audit fields the capability registry needs into tools/skills/data/.

Run only when the audit or catalog pin changes (see the re-audit procedure in
docs/superpowers/plans/2026-10-05-scientific-skills-expansion.md):

    python3 tools/skills/export_audit.py --audit-dir <audit root> --catalog-clone <git clone>

The audit and clone live outside Git. The output keeps only short structured fields,
catalog paths and SHA-256 hashes: no evidence quotes, local paths or secrets.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
WATCH_BRANCHES = ["claude/microfluidic-design-skill-1o5uz6", "feat/google-deep-research", "orion/add-k-dense-web-gif"]
PY_HINT = re.compile(r"(?:<\s?3\.1[0-4]|<=\s?3\.1[0-3]|3\.\d+\s?[–-]\s?3\.1[0-3]\b|python\s?3\.(?:9|10|11|12|13)\b|x86[-_]64|amd64|rosetta)", re.I)


def short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def git(clone: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=clone, check=True, capture_output=True, text=True).stdout


def skill_names(clone: Path, ref: str) -> set[str]:
    return {p.split("/")[1] for p in git(clone, "ls-tree", "-r", "--name-only", ref, "skills/").split()
            if p.endswith("/SKILL.md") and p.count("/") == 2}


def record(r: dict) -> dict:
    q, rc, lic = r["requirements"], r["recommendations"], r["license_review"]
    terms = lic["skill_terms"] + " " + " ".join(x["terms"] for x in lic["dependency_and_data_terms"])
    hint_text = json.dumps([q["dependencies"], r["source_facts"], rc["blockers"], r["uncertainties"]], ensure_ascii=False)
    io = {short(t, 40).lower() for side in ("inputs", "outputs") for o in q[side] for t in [o["kind"], *o["formats"]]}
    return {
        "path": r["source"]["path"],
        "sha256": r["source"]["sha256"],
        "families": r["families"],
        "profile": rc["executor_profile"],
        "priority": rc["priority"],
        "readiness": rc["readiness"],
        "compute": q["compute"]["class"],
        "hardware": sorted({short(h if isinstance(h, str) else json.dumps(h), 100) for h in q["compute"]["hardware"]}),
        "dependencies": sorted({(short(d["name"], 40), d["role"], short(d["version"], 40)) for d in q["dependencies"]}),
        "python_hints": sorted({short(d["version"], 60) for d in q["dependencies"] if "python" in d["name"].lower()[:8]}
                               | {m.group(0).lower().replace(" ", "") for m in PY_HINT.finditer(hint_text)}),
        "network": sorted({(short(n["service"], 40), n.get("cost", "unclear")) for n in q["network"]}),
        "credentials": sorted({(short(c["name"], 50), c.get("required", "")) for c in q["credentials"]}),
        "io_terms": sorted(io)[:40],
        "output_terms": sorted({short(t, 40).lower() for o in q["outputs"] for t in [o["kind"], *o["formats"]]})[:30],
        "backend_components": sorted({short(b["component"], 50) for b in rc["backend"]}),
        "acceptance_tests": [{"name": short(a["name"], 80), "mode": a["mode"], "status": a["status"]} for a in rc["acceptance_tests"]],
        "blockers": [short(b, 140) for b in rc["blockers"][:3]],
        "license": {"skill_terms": short(lic["skill_terms"], 80), "commercial_use": lic["commercial_use"],
                    "noncommercial_asset": bool(re.search(r"non.?commercial|by-nc|polyform", terms, re.I)),
                    "copyleft_dependency": bool(re.search(r"\b(?:A?GPL|LGPL)", terms))},
        "semantic_reviewed": r["review"]["validation"].endswith("semantic_sample_pass"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit-dir", type=Path, required=True)
    ap.add_argument("--catalog-clone", type=Path, required=True)
    a = ap.parse_args()
    cov = json.loads((a.audit_dir / "report/coverage.json").read_text())
    pin = cov["catalog_commit"]
    inv = {s["id"]: s["sha256"] for s in json.loads((a.audit_dir / "inventory.json").read_text())["skills"]}
    with (a.audit_dir / "report/integration-matrix.csv").open(encoding="utf-8-sig") as f:
        matrix = {row["skill"] for row in csv.DictReader(f)}
    pinned = skill_names(a.catalog_clone, pin)
    recs = {r["skill_key"]: r for r in cov["records"]}
    assert set(recs) == pinned == set(inv) == matrix, "audit keys differ from pinned catalog"
    assert all(inv[k] == recs[k]["source"]["sha256"] for k in pinned), "SKILL.md hash mismatch"
    main_tip = git(a.catalog_clone, "rev-parse", "origin/main").strip()
    watch = []
    for b in WATCH_BRANCHES:
        names = skill_names(a.catalog_clone, f"origin/{b}")
        stale = subprocess.run(["git", "merge-base", "--is-ancestor", f"origin/{b}", pin], cwd=a.catalog_clone).returncode == 0
        watch.append({"branch": b, "tip": git(a.catalog_clone, "rev-parse", "--short", f"origin/{b}").strip(),
                      "skill_count": len(names), "adds": sorted(names - pinned), "drops": len(pinned - names),
                      "action": "stale (ancestor of pin, old layout); ignore" if stale else "watch only; not on main; do not import"})
    DATA.mkdir(parents=True, exist_ok=True)
    catalog = {"repository": "K-Dense-AI/scientific-agent-skills", "commit": pin, "skill_count": len(pinned),
               "upstream_main_at_check": main_tip, "checked_on": cov.get("checked_on", "2026-10-05"),
               "watch_branches": watch}
    audit = {"audit_id": "scientific-skills-audit-2026-10-04", "platform_snapshot": cov["platform_snapshot"],
             "scope": cov["scope"], "mechanical_validator": "PASS",
             "records": {k: record(recs[k]) for k in sorted(pinned)}}
    for name, obj in (("catalog.json", catalog), ("audit-records.json", audit)):
        (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n")
    print("exported", len(pinned), "records at", pin[:12])


if __name__ == "__main__":
    main()
