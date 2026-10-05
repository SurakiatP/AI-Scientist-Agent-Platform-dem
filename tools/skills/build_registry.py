#!/usr/bin/env python3
"""Build docs/skills/capability-registry.json and docs/skills/coverage.md, and refresh the
wave lists in the expansion plan, from tracked data only (tools/skills/data/*.json).

    python3 tools/skills/build_registry.py            # write outputs
    python3 tools/skills/build_registry.py --check    # exit 1 if outputs differ from a fresh build
    python3 tools/skills/build_registry.py --verify-platform   # also assert recorded platform facts still hold

Deterministic: output depends only on the data files and the rules below (no git HEAD, no clock).
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = HERE / "data"
OUT_JSON = ROOT / "docs/skills/capability-registry.json"
OUT_MD = ROOT / "docs/skills/coverage.md"
OUT_PLAN = ROOT / "docs/superpowers/plans/2026-10-05-scientific-skills-expansion.md"
STATUS_KEYS = ["analyzed", "implemented", "validated", "enabled", "blocked"]

# --------------------------------------------------------------------------- catalogues
# gates: "merge" = must hold before integration merge; "ui" = needed only for product UI exposure.
WAVES = {
    "X0": ("Capability foundation: registry, task-relevant instruction loader, typed tool host, contained recipe runner, artifact manifest, profile build pipeline",
           {"merge": ["main:B5 accepted", "main:I1-host live-accepted"], "ui": []},
           "Design/contracts/tests may run now in parallel with I1/I2 in non-overlapping files. Does NOT wait on B6 paper live acceptance, I2, F4, I3 or I4."),
    "X1": ("Evidence, writing and reasoning (public scholarly retrieval, citations, document ingestion, instruction-guided drafting)",
           {"merge": ["X0"], "ui": ["main:F4"]}, "Backend proceeds after X0; source/citation UI exposure after F4."),
    "X2": ("Core CPU analysis (tables, statistics, ML, time series, units, symbolic math, plots, document export)",
           {"merge": ["X0", "image:prof.cpu-sci build/scan PASS"], "ui": ["main:F4"]}, ""),
    "X3": ("Public scientific data sources (credential-free REST/gRPC/MCP databases)",
           {"merge": ["X1", "main:I1 (outbound MCP client reuses its SDK pin and policy, MCP-only sources only)"], "ui": ["main:F4"]}, ""),
    "X4": ("Domain CPU/native analysis (omics, chemistry/materials, imaging/spatial/neuro, simulation, lab data formats)",
           {"merge": ["X2", "image: per-profile build/scan PASS"], "ui": ["main:F4", "domain viewers"]}, "Sub-batches by profile image."),
    "X5": ("Large data and dataset staging (out-of-core, chunked stores, versioned dataset fetch)",
           {"merge": ["X4", "main:I3 encrypted volumes + backup/restore"], "ui": ["main:F4"]}, ""),
    "X6": ("GPU and model-weight workloads (CPU-path tests allowed; never counted as GPU validation)",
           {"merge": ["X2", "X4 (shared profiles)"], "ui": ["main:F4"]}, "GPU validation additionally needs pre.gpu.cuda_host."),
    "X7": ("Credentialed, paid and remote-compute services (off by default; one service at a time)",
           {"merge": ["main:I2 outbound reconciliation", "main:I3", "main:I4 security evidence", "core.remote_job_ledger"], "ui": ["main:F4"]},
           "Live validation needs each service's credential and spend-ceiling prerequisites."),
    "X8": ("External labs, ELN/LIMS repositories and instruments (simulation first)",
           {"merge": ["X7"], "ui": ["main:F4"]}, ""),
    "X9": ("Specialist-gated clinical, regulatory and welfare capabilities",
           {"merge": ["X2"], "ui": ["main:F4"]}, "Software validation may pass; enablement needs separate recorded signoff."),
    "X10": ("Held: policy or license conflict; not adopted until the named unlock condition is met", {"merge": ["owner decision"], "ui": []}, ""),
}
CORE = {
    "core.registry": "Capability registry: id, catalog pin, instruction hashes, tool schemas, profile image digest, prerequisites, status evidence",
    "core.instruction_loader": "Task-relevant loader: server-side shortlist from registry cards, owner-approved plan binding, hash-verified section loading under a token budget",
    "core.typed_tool_host": "Reviewed typed tools in the worker (schema-validated, journaled, deny-by-default) beyond todo_list",
    "core.artifact_manifest": "Scientific artifact manifest: inputs/versions, parameters, seeds, units, package versions, prediction vs measurement",
    "core.resource_reservation": "CPU/memory/disk/time reservation per profile image, tied to run budget and cancellation",
    "core.connection_grants": "Per-service connection grant: validated destination, secret reference, scopes, spend ceiling, revocation",
    "core.release_approval": "Explicit owner release approval before user data leaves the deployment",
    "core.remote_job_ledger": "External job submit/poll/cancel/reconcile with unknown-outcome owner-wait and no-resend",
    "core.large_object_staging": "Chunked object staging/streaming through MinIO with bounded readers and checksums",
}
ADAPTERS = {
    "ad.instructions_only": "No tool; loader-only guidance producing a structured text artifact",
    "ad.scholarly_retrieval": "Typed scholarly API retrieval via broker: pagination, dedup, identifiers, abstract/full-text labels",
    "ad.citation": "Citation verification (existing research.verify_citation) plus BibTeX/CSL export",
    "ad.doc_ingest": "Local document parsing/conversion with layout provenance",
    "ad.public_data_api": "Typed adapters for public scientific REST/gRPC endpoints with rate limits and version pins",
    "ad.mcp_client": "Outbound MCP client to approved remote servers through the broker",
    "ad.tabular_io": "Tabular reader/validator (CSV/TSV/Parquet/Excel) with schema, units and missingness checks",
    "ad.sci_format_io": "Bounded scientific format readers/writers (FASTA/BAM/VCF/BED/H5AD/Zarr/FITS/FCS/NWB/DICOM/mzML/SDF/PDB)",
    "ad.contained_recipe": "Contained execution of reviewed recipes in the skill's profile image (no network, seeded, manifest output)",
    "ad.plot_render": "Static figure rendering (SVG/PNG) with units/legend/method captions",
    "ad.doc_export": "Document export (PDF/PPTX/LaTeX/Markdown) with pinned renderers",
    "ad.dataset_fetch": "Versioned public dataset/model download with checksum, license check and staging",
    "ad.model_weights": "Pinned model-weight acquisition/cache with license and gated-access checks",
    "ad.gpu_executor": "GPU job execution with device admission, memory limits and cancellation",
    "ad.remote_compute": "Remote compute/job service adapter (submit, poll, cancel, fetch results)",
    "ad.paid_api": "Paid hosted API adapter with per-call cost estimate and spend ceiling",
    "ad.eln_repository": "ELN/LIMS/repository connectors (read first, scoped writes after approval)",
    "ad.lab_order": "External lab/manufacturing quote and order submission with owner approval",
    "ad.instrument_control": "Instrument protocol simulation; physical run only with operator",
}
FRONTEND = {
    "fe.plan_review": "Plan review with capability steps, versions, prerequisites, release and cost",
    "fe.progress": "Phase progress from durable state",
    "fe.report_viewer": "Markdown/math report viewer (exists)",
    "fe.sources": "Sources/citations with identifier/access provenance (Library exists)",
    "fe.composer_inputs": "Schema-driven scientific input metadata (outcome/group/units/organism/build)",
    "fe.table_viewer": "Table viewer with units/filters/export",
    "fe.chart_viewer": "Figure/chart viewer with legend/method/uncertainty",
    "fe.doc_viewer": "PDF/PPTX preview and download",
    "fe.sequence_viewer": "Sequence/alignment/genome-track viewer",
    "fe.structure_viewer": "Molecule/structure viewer",
    "fe.tree_network_viewer": "Phylogenetic tree / graph viewer",
    "fe.image_viewer": "Image/microscopy/spatial/astronomy viewer (tiled)",
    "fe.connections_settings": "Connection grants, keys and spend ceilings in Settings",
    "fe.job_status": "External job status, cancel and reconciliation",
    "fe.readiness_notice": "Unavailable capability notice with reason and unlock condition",
}
BUILD_SCAN = ["hash-locked lockfile", "actual image build", "Syft SBOM", "dated Trivy scan: 0 HIGH/CRITICAL or recorded exception",
              "license inventory", "containment run (no network, read-only, cap-drop, limits)"]
PROFILES = {  # default python per profile; per-skill overrides become separate images
    "prof.none": ("No execution image (instructions only)", None),
    "prof.worker-base": ("Existing worker image (Python 3.14.7); typed retrieval adapters run server-side in the broker (Python 3.13)", "3.14.7"),
    "prof.doc-ingest": ("Document parsing (markitdown/liteparse, OCR natives)", "3.13"),
    "prof.doc-export": ("Document export (TeX, python-pptx, renderers)", "3.13"),
    "prof.cpu-sci": ("CPU scientific Python (NumPy/SciPy/pandas/statsmodels/scikit-learn/PyMC/Polars/matplotlib)", "3.13"),
    "prof.omics": ("Omics natives (HTSlib/pysam, scanpy/anndata, pydeseq2, scikit-bio, OpenMS)", "3.13"),
    "prof.chem": ("Chemistry/materials (RDKit, datamol, medchem, matchms, pymatgen, OpenMM)", "3.13"),
    "prof.imaging": ("Imaging/spatial (astropy, GDAL/GeoPandas, OpenSlide, CellProfiler, SpikeInterface, pydicom)", "3.13"),
    "prof.sim": ("Simulation/engineering (Cantera, PyBaMM, Tellurium, QuTiP, Qiskit Aer, Cirq, SimPy, CFD)", "3.13"),
    "prof.large-data": ("Out-of-core/chunked data (Dask, Vaex, Zarr, TileDB, DataLad/git-annex)", "3.13"),
    "prof.gpu-ml": ("PyTorch stack (Transformers, PyG, scvi-tools, ESM); CPU-path image + CUDA image", "3.13"),
    "prof.remote-client": ("Thin pinned SDK clients for remote services (no local compute)", "3.13"),
    "prof.lab-sim": ("Instrument simulators (Opentrons/PyLabRobot) without hardware", "3.13"),
}
# Per-skill Python where the audit records a narrower supported/tested range than the profile default.
PY_TARGET = {
    "13c-metabolic-flux": "3.12", "pybamm": "3.12", "nwb-conversion": "3.12", "geniml": "3.12", "vaex": "3.12",
    "pathml": "3.12", "omero-integration": "3.12", "latchbio-integration": "3.12", "tiledbvcf": "3.12", "pyhealth": "3.12",
    "molfeat": "3.12", "qiime2-amplicon": "3.12",
    "deepchem": "3.11", "histolab": "3.11", "arboreto": "3.11", "pytdc": "3.11", "tellurium": "3.11",
    "torchdrug": "3.10", "diffdock": "3.9",
}
AMD64_ONLY = {"qiime2-amplicon": "official QIIME 2 distribution is linux/amd64 (emulation on arm64 host)",
              "pufferlib": "native 5 Raylib archive is amd64"}
OWN_ENV = {"flowkit": "pandas<3", "neurokit2": "pandas<3", "geniml": "zarr<3", "vaex": "pandas<3, Dask<2024.9",
           "scvelo": "pinned pandas/NumPy", "scikit-bio": "pandas 3 (conflicts with FlowKit/NeuroKit2)"}
ASSET_HOLDS = {  # ADR-016 Q3: restricted assets held; permissive path of the same skill may proceed
    "timesfm-forecasting": "TimesFM 3.0 weights (non-commercial/non-production) held; Apache-2.0 2.5 weights allowed",
    "imaging-data-commons": "CC BY-NC series held; per-series license filter allows CC BY series only",
    "medchem": "ChEMBL/NIBR/PAINS/ZINC-derived catalog terms to review before bundling",
}
EOL_PY = {"3.9": "Python 3.9 is end-of-life: needs a recorded security exception or an upstream port"}
CPU_PATH = {"deepspot-m", "diffdock", "esm", "pufferlib", "pytorch-lightning", "scvi-tools", "timesfm-forecasting",
            "torch-geometric", "transformers", "waypoint-bio"}
TESTS = {
    "at.loader_selection": "Only selected instructions load, hashes match, token budget enforced, names hidden in UI",
    "at.tool_contract": "Typed tool schema, journaling, deny-by-default and cancellation",
    "at.profile_build": "Image build/scan per BUILD_SCAN list",
    "at.containment_profile": "Profile runs under existing containment (no direct network/secrets)",
    "at.scientific_fixture": "Skill-specific scientific input/output check on the real runtime (audit acceptance tests)",
    "at.retrieval_live": "Owner-configured live retrieval with pagination/completeness/provenance",
    "at.remote_reconcile": "Remote job submit/crash/unknown-outcome/no-resend/cancel reconciliation",
    "at.hardware_sim": "Simulator protocol check before any physical run",
    "at.cpu_path": "Upstream-supported CPU path fixture (does not count as GPU validation)",
    "at.gpu_run": "Representative GPU run on a reviewed CUDA host",
}
PREREQ_TYPES = {
    "credential": ("Owner account/API key/tenant required", "Owner configures a connection grant and its readiness check passes"),
    "paid_service": ("Paid service", "Owner sets a spend ceiling; service enabled alone, one at a time"),
    "gpu": ("CUDA GPU host required for GPU validation", "A reviewed CUDA host exists and at.gpu_run passes; CPU-path tests never count"),
    "hardware": ("Physical lab hardware/operator required", "Target device, operator and safety review; at.hardware_sim first"),
    "license": ("Restricted license/terms", "Terms reviewed and approved for a commercial-capable deployment"),
    "specialist_review": ("Qualified scientific/clinical signoff required", "Named qualified reviewer records signoff (separate from software tests)"),
    "upstream_defect": ("Known upstream defect or gated model access", "Upstream fix reaches main and the re-audit passes"),
    "policy_conflict": ("Conflicts with approved runtime authority/containment", "See detail"),
}
STAGES = ["implement", "validate", "gpu_validation", "enable"]

# --------------------------------------------------------------------------- curated overrides (ids/rules, not audit prose)
WAVE_OVERRIDE = {
    "get-available-resources": "X0",
    "markitdown": "X1", "liteparse": "X1", "markdown-mermaid-writing": "X1", "literature-review": "X1",
    "latex-posters": "X2", "pptx-posters": "X2", "scientific-slides": "X2", "matplotlib": "X2", "seaborn": "X2",
    "scientific-visualization": "X2", "experimental-design": "X2", "statistical-power": "X2",
    "anndata": "X4", "pysam": "X4", "polars-bio": "X4", "pyopenms": "X4", "bulk-rnaseq": "X4", "neurokit2": "X4",
    "qiskit": "X4", "cirq": "X4", "pennylane": "X4", "pathml": "X4",
    "folklore-variant-evidence": "X3", "protocolsio-integration": "X3", "hugging-science": "X3",
    "datalad": "X5", "cellxgene-census": "X5", "depmap": "X5", "primekg": "X5", "imaging-data-commons": "X5",
    "paperclip": "X7", "nextflow": "X7", "pacsomatic": "X7", "hypogenic": "X7", "omero-integration": "X8",
    "pydicom": "X9", "relsa-severity-assessment": "X9", "pkpd-modeling": "X9", "analytical-method-validation": "X9", "pyhealth": "X9",
    "matlab": "X10", "pi-agent": "X10", "autoskill": "X10", "what-if-oracle": "X10", "waypoint-bio": "X10",
    "fictiv": "X10", "ginkgo-cloud-lab": "X10",
}
EXTRA_PREREQS = {  # (type, detail, stage, unlock override or None)
    "opentrons-integration": [("hardware", "Target OT-2/Flex robot, labware and operator", "enable", None)],
    "pylabrobot": [("hardware", "Physical liquid handler, calibration and operator", "enable", None)],
    "what-if-oracle": [("license", "CC BY-NC-SA 4.0 instructions; commercial use needs author license", "implement", None)],
    "deepspot-m": [("license", "PolyForm-Noncommercial; gated weights", "implement", None)],
    "alphagenome": [("license", "AlphaGenome API terms: non-commercial research only", "implement", None)],
    "matlab": [("license", "MATLAB/toolbox proprietary license; GNU Octave (GPL) alternative unreviewed", "implement", None)],
    "waypoint-bio": [("upstream_defect", "TaxonomicTokenizer.save_pretrained defect; gated per-repo access; Transformers 5 incompatibility", "implement", None)],
    "pi-agent": [("policy_conflict", "Unsandboxed coding-agent tools conflict with broker-only runtime authority", "implement",
                  "Owner accepts a contained design that preserves broker-only authority (none proposed)")],
    "autoskill": [("policy_conflict", "Reads host screen/history (Screenpipe); privacy and authority conflict", "implement",
                   "Owner accepts a contained design without host-history access (none proposed)")],
    "fictiv": [("policy_conflict", "Logged-in browser session only; generic browser control is disabled", "implement",
                "Provider API exists, or owner approves a reviewed supervised-browser adapter design")],
    "ginkgo-cloud-lab": [("policy_conflict", "No published submission API; browser ordering only", "implement",
                          "Provider API exists, or owner approves a reviewed supervised-browser adapter design")],
    "nextflow": [("policy_conflict", "Nested container/scheduler execution", "implement",
                  "Reviewed remote-executor design with isolation (ADR-016 Q4)")],
    "pacsomatic": [("policy_conflict", "Nextflow/nf-core pipeline needs nested containers", "implement",
                    "Reviewed remote-executor design with isolation (ADR-016 Q4)")],
    "genomic-intelligence": [("credential", "GI account/key; pricing and DNA-confidentiality terms unresolved", "validate", None)],
    "open-notebook": [("credential", "Owner-deployed Open Notebook instance and login", "validate", None)],
    "paperclip": [("credential", "GXL Paperclip account/API key or OAuth", "validate", None)],
    "parallel-web": [("credential", "PARALLEL_API_KEY or OAuth", "validate", None)],
    "research-lookup": [("credential", "PARALLEL_API_KEY and/or OPENROUTER_API_KEY", "validate", None)],
    "hypogenic": [("paid_service", "Hosted LLM calls outside the approved provider path", "validate", None)],
}
ADAPTER_OVERRIDE = {
    "get-available-resources": ["ad.contained_recipe"],
    "markitdown": ["ad.doc_ingest"], "liteparse": ["ad.doc_ingest"],
    "citation-management": ["ad.citation", "ad.scholarly_retrieval"],
    "paper-lookup": ["ad.citation", "ad.scholarly_retrieval"],
    "literature-review": ["ad.citation", "ad.doc_ingest", "ad.scholarly_retrieval"],
    "scientific-writing": ["ad.citation", "ad.doc_export"],
    "pyzotero": ["ad.eln_repository"], "protocolsio-integration": ["ad.mcp_client", "ad.public_data_api"],
    "open-notebook": ["ad.eln_repository"], "omero-integration": ["ad.eln_repository", "ad.sci_format_io"],
    "benchling-integration": ["ad.eln_repository"], "labarchive-integration": ["ad.eln_repository"],
    "opentrons-integration": ["ad.instrument_control"], "pylabrobot": ["ad.instrument_control"],
    "lab-hardware-cad": ["ad.contained_recipe", "ad.doc_export"],
    "bgpt-paper-search": ["ad.mcp_client", "ad.scholarly_retrieval"], "folklore-variant-evidence": ["ad.mcp_client"],
    "generate-image": ["ad.paid_api"], "infographics": ["ad.doc_export", "ad.paid_api"], "scientific-schematics": ["ad.paid_api"],
    "nextflow": ["ad.remote_compute"], "pacsomatic": ["ad.remote_compute"],
    "matlab": ["ad.contained_recipe"], "pi-agent": [], "autoskill": [],
    "fictiv": ["ad.lab_order"], "ginkgo-cloud-lab": ["ad.lab_order"], "adaptyv": ["ad.lab_order"],
    "hugging-science": ["ad.dataset_fetch", "ad.public_data_api"], "hypogenic": ["ad.contained_recipe", "ad.paid_api"],
}
FAM_SHORT = {"literature_evidence": "literature", "database_lookup": "databases", "statistics_data": "statistics",
             "timeseries": "time series", "writing_visualization": "writing/viz", "imaging_spatial": "imaging/spatial",
             "genomics_omics": "genomics/omics", "molecular_chemistry": "molecular/chem", "simulation_engineering": "simulation",
             "laboratory_workflows": "lab workflows", "clinical_regulatory": "clinical/regulatory", "remote_services": "remote services",
             "research_planning": "research planning", "developer_tools": "developer tools"}


def short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- derivation
def assign_wave(k, r):
    if k in WAVE_OVERRIDE:
        return WAVE_OVERRIDE[k]
    prof, fam = r["profile"], r["families"][0]
    req = [c for c in r["credentials"] if c[1] == "yes"]
    paid = any(n[1] == "paid" for n in r["network"])
    if r["priority"] == "blocked":
        return "X10"
    if fam == "clinical_regulatory":
        return "X9"
    if prof == "external_lab":
        return "X8"
    if prof == "remote_compute" or req or (paid and prof == "literature_broker"):
        return "X7"
    if prof == "gpu_analysis":
        return "X6"
    if prof == "large_data":
        return "X5"
    if prof == "literature_broker":
        return "X1" if fam == "literature_evidence" else "X3"
    if prof in ("instruction_only", "report_generation"):
        return "X1"
    if prof == "cpu_analysis" and fam in ("statistics_data", "timeseries", "research_planning", "developer_tools"):
        return "X2"
    return "X4"


def prereqs_for(k, r, wave):
    out = []
    req = [c[0] for c in r["credentials"] if c[1] == "yes"]
    if req:
        out.append(("credential", short(", ".join(req), 80), "validate", None))
    paid = sorted({n[0] for n in r["network"] if n[1] == "paid"})
    if paid and (wave in ("X7", "X8") or req):
        out.append(("paid_service", short(", ".join(paid), 80), "validate", None))
    if r["profile"] == "gpu_analysis" or wave == "X6":
        cpu = k in CPU_PATH
        out.append(("gpu", "CUDA host + model weights" + ("; upstream CPU path testable now" if cpu else "; no CPU path"),
                    "gpu_validation" if cpu else "validate", None))
    if r["readiness"] == "specialist_review_needed":
        out.append(("specialist_review", short(r["blockers"][0] if r["blockers"] else "qualified reviewer", 90), "enable", None))
    if r["profile"] == "external_lab" and not req and k not in EXTRA_PREREQS:
        out.append(("credential", "Owner lab/service account", "validate", None))
    for p in EXTRA_PREREQS.get(k, []):
        if p[0] not in {x[0] for x in out}:
            out.append(p)
    res = []
    for t, detail, stage, unlock in sorted(out):
        pid = "pre.gpu.cuda_host" if t == "gpu" else f"pre.{t}.{k}"
        res.append({"id": pid, "type": t, "detail": detail, "gates": stage, "unlock": unlock or PREREQ_TYPES[t][1]})
    return res


def adapters_for(k, r, wave):
    if k in ADAPTER_OVERRIDE:
        return sorted(ADAPTER_OVERRIDE[k])
    prof, fam = r["profile"], r["families"][0]
    nets = " ".join(n[0] for n in r["network"]).lower()
    io = " ".join(r["io_terms"])
    out_terms = " ".join(r["output_terms"])
    s = set()
    if prof == "instruction_only":
        s.add("ad.instructions_only")
    if prof == "literature_broker":
        s.add("ad.scholarly_retrieval" if fam == "literature_evidence" else "ad.public_data_api")
    if "mcp" in nets:
        s.add("ad.mcp_client")
    if prof in ("cpu_analysis", "scientific_native", "large_data", "gpu_analysis", "developer_tooling"):
        s.add("ad.contained_recipe")
        if re.search(r"csv|tsv|parquet|table|dataframe|xlsx", io):
            s.add("ad.tabular_io")
        if fam not in ("statistics_data", "research_planning") or re.search(r"fasta|bam|vcf|h5ad|zarr|fits|fcs|nwb|dicom|mzml|sdf|pdb|tiff", io):
            s.add("ad.sci_format_io")
    if prof == "large_data" and r["network"]:
        s.add("ad.dataset_fetch")
    if prof == "gpu_analysis":
        s.add("ad.gpu_executor")
        if re.search(r"hugging|weights|checkpoint|model", nets + " " + " ".join(d[0] for d in r["dependencies"]).lower()):
            s.add("ad.model_weights")
    if prof == "remote_compute":
        s.add("ad.remote_compute")
    if prof == "external_lab":
        s.add("ad.lab_order")
    if prof == "report_generation":
        s.add("ad.doc_export")
    if re.search(r"\bpng\b|\bsvg\b|plot|figure|chart", out_terms) and prof != "instruction_only":
        s.add("ad.plot_render")
    if any(n[1] == "paid" for n in r["network"]) and wave in ("X7", "X8"):
        s.add("ad.paid_api")
    return sorted(s)


def core_for(r, adapters, wave):
    s = {"core.registry", "core.instruction_loader", "core.artifact_manifest"}
    if adapters and adapters != ["ad.instructions_only"]:
        s.add("core.typed_tool_host")
    if {"ad.contained_recipe", "ad.gpu_executor"} & set(adapters):
        s.add("core.resource_reservation")
    if r["network"] or r["credentials"]:
        s.add("core.connection_grants")
    if {"ad.remote_compute", "ad.lab_order", "ad.paid_api", "ad.eln_repository", "ad.instrument_control"} & set(adapters):
        s.update({"core.remote_job_ledger", "core.release_approval"})
    if wave == "X5" or "ad.dataset_fetch" in adapters:
        s.add("core.large_object_staging")
    return sorted(s)


def frontend_for(r, adapters, blocked):
    io = " ".join(r["io_terms"])
    s = {"fe.plan_review", "fe.progress", "fe.report_viewer"}
    pats = {
        "fe.table_viewer": r"csv|tsv|table|parquet|dataframe|xlsx",
        "fe.chart_viewer": r"\bpng\b|\bsvg\b|plot|chart|figure",
        "fe.doc_viewer": r"\bpdf\b|pptx|poster|slide",
        "fe.sequence_viewer": r"fasta|fastq|\bbam\b|\bvcf\b|\bbed\b|bigwig|gff|alignment|genbank",
        "fe.structure_viewer": r"\bpdb\b|mmcif|\bsdf\b|smiles|molecule|structure",
        "fe.tree_network_viewer": r"newick|\bnhx\b|phylo|graph|network",
        "fe.image_viewer": r"tiff|dicom|fits|image|slide|raster|microscop",
    }
    s.update(k for k, p in pats.items() if re.search(p, io))
    if {"ad.scholarly_retrieval", "ad.citation"} & set(adapters):
        s.add("fe.sources")
    if r["io_terms"] and r["profile"] not in ("instruction_only", "literature_broker"):
        s.add("fe.composer_inputs")
    if r["credentials"] or any(n[1] == "paid" for n in r["network"]):
        s.add("fe.connections_settings")
    if {"ad.remote_compute", "ad.lab_order", "ad.instrument_control"} & set(adapters):
        s.add("fe.job_status")
    if blocked:
        s.add("fe.readiness_notice")
    return sorted(s)


def profile_for(k, r, wave):
    prof, fam = r["profile"], r["families"][0]
    if prof == "instruction_only":
        return "prof.none"
    if k in ("markitdown", "liteparse"):
        return "prof.doc-ingest"
    if prof == "literature_broker":
        return "prof.worker-base"
    if prof in ("remote_compute", "external_lab") or wave in ("X7", "X8"):
        return "prof.lab-sim" if k in ("opentrons-integration", "pylabrobot") else "prof.remote-client"
    if prof == "gpu_analysis" or wave == "X6":
        return "prof.gpu-ml"
    if prof == "report_generation":
        return "prof.doc-export" if re.search(r"pptx|latex|slide|poster|pdf", k + " " + " ".join(r["output_terms"])) else "prof.cpu-sci"
    if prof == "large_data" and wave == "X5":
        return "prof.large-data"
    return {"genomics_omics": "prof.omics", "molecular_chemistry": "prof.chem", "imaging_spatial": "prof.imaging",
            "simulation_engineering": "prof.sim"}.get(fam, "prof.imaging" if k == "bids" else "prof.cpu-sci")


def runtime_for(k, profile):
    default = PROFILES[profile][1]
    py = PY_TARGET.get(k, default)
    arch = "linux/amd64" if k in AMD64_ONLY else ("n/a" if profile == "prof.none" else "linux/arm64+linux/amd64")
    notes = [n for n in (AMD64_ONLY.get(k), OWN_ENV.get(k) and f"own env: {OWN_ENV[k]}", EOL_PY.get(py)) if n]
    image = None if profile == "prof.none" else f"{profile}@py{py}" + ("-amd64" if k in AMD64_ONLY else "") + (f"+{k}" if k in OWN_ENV or (py != default) else "")
    return {"python": py, "arch": arch, "image": image, "isolated_env": k in OWN_ENV or py != default, "notes": notes}


def license_status(r, prereqs):
    L = r["license"]
    if any(p["type"] == "license" for p in prereqs):
        return "restricted"
    if L["noncommercial_asset"]:
        return "restricted_option"
    if L["copyleft_dependency"]:
        return "copyleft_review"
    return "review_required" if L["commercial_use"] == "requires_review" else "not_concluded"


def ev_slot(evidence, key, stage):
    e = (evidence.get(key) or {}).get(stage) or {}
    return e.get("evidence"), e.get("date")


def build():
    catalog, audit, ledger = load("catalog.json"), load("audit-records.json"), load("status-evidence.json")
    recs = audit["records"]
    evidence = ledger["skills"]
    data_sha = hashlib.sha256(b"".join((DATA / n).read_bytes() for n in sorted(p.name for p in DATA.glob("*.json")))).hexdigest()
    pre_catalog = {}
    skills = {}
    for k in sorted(recs):
        r = recs[k]
        wave = assign_wave(k, r)
        pre = prereqs_for(k, r, wave)
        adapters = adapters_for(k, r, wave)
        core = core_for(r, adapters, wave)
        fe = frontend_for(r, adapters, bool(pre))
        profile = profile_for(k, r, wave)
        rt = runtime_for(k, profile)
        for p in pre:
            entry = pre_catalog.setdefault(p["id"], {"type": p["type"], "detail": p["detail"] if p["id"] != "pre.gpu.cuda_host" else
                                                      "Reviewed CUDA host (current host is Apple M4 arm64 without CUDA)",
                                                      "unlock": p["unlock"], "skills": []})
            entry["skills"].append(k)
        gates = {s: [p["id"] for p in pre if p["gates"] == s] for s in STAGES}
        tests = ["at.loader_selection", "at.scientific_fixture"]
        if adapters and adapters != ["ad.instructions_only"]:
            tests += ["at.tool_contract", "at.profile_build", "at.containment_profile"]
        if wave in ("X1", "X3") and r["network"]:
            tests.append("at.retrieval_live")
        if wave == "X7" or (wave == "X8" and k not in ("opentrons-integration", "pylabrobot")):
            tests.append("at.remote_reconcile")
        if k in ("opentrons-integration", "pylabrobot"):
            tests.append("at.hardware_sim")
        gpu_applicable = profile == "prof.gpu-ml"
        if gpu_applicable:
            tests += (["at.cpu_path"] if k in CPU_PATH else []) + ["at.gpu_run"]
        is_bundled = k in ("paper-lookup", "literature-review", "scientific-writing")
        skill_tests = [a["name"] for a in r["acceptance_tests"]]

        def status(stage, reason, unlock, blockers=()):
            ev, date = ev_slot(evidence, k, stage)
            return {"value": ev is not None and not blockers, "evidence": ev, "date": date,
                    "reason": "evidence recorded" if ev and not blockers else reason, "unlock": None if ev and not blockers else unlock}

        impl_unlock = (f"{'; '.join(pre_catalog[p]['unlock'] for p in gates['implement'])}" if gates["implement"] else
                       f"{wave} merged: loader entry + {', '.join(a[3:] for a in adapters) or 'card only'}"
                       + (f" + image {rt['image']} build/scan PASS" if rt["image"] else ""))
        val_unlock = "implemented; " + "; ".join(
            [f"pass {', '.join(skill_tests[:3])} on real runtime"] +
            [pre_catalog[p]["unlock"] for p in gates["validate"]] +
            (["owner-configured live run"] if "at.retrieval_live" in tests or "at.remote_reconcile" in tests else []))
        en_unlock = "validated; " + "; ".join(
            [pre_catalog[p]["unlock"] for p in gates["enable"]] + ["registry switch in deployment config (dated, image digest)"])
        statuses = {
            "analyzed": {"value": True, "evidence": f"audit:{audit['audit_id']}#{k}", "date": "2026-10-04",
                         "reason": "mechanical validator PASS" + ("; semantic sample PASS" if r["semantic_reviewed"] else "; not semantically reviewed"),
                         "unlock": None},
            "implemented": status("implemented",
                                  "SKILL.md bundled as files only; no loader entry or typed tool" if is_bundled else "no loader entry, adapter or profile image",
                                  impl_unlock, gates["implement"]),
            "validated": status("validated", "no acceptance run on the real runtime (audit tests proposed_not_run)" +
                                ("; paper-workflow live acceptance pending" if is_bundled else ""), val_unlock, gates["implement"] + gates["validate"]),
            "enabled": status("enabled", "no capability registry in any deployment", en_unlock,
                              gates["implement"] + gates["validate"] + gates["enable"]),
            "blocked": {"value": bool(pre), "by": sorted({p["type"] for p in pre}), "evidence": [p["id"] for p in pre],
                        "reason": "; ".join(f"{p['type']}: {p['detail']}" for p in pre) if pre else "no external prerequisite; engineering only",
                        "unlock": "; ".join(dict.fromkeys(p["unlock"] for p in pre)) or None},
        }
        sign_ev, sign_date = ev_slot(evidence, k, "scientific_signoff")
        gpu_ev, gpu_date = ev_slot(evidence, k, "gpu_validation")
        cpu_ev, cpu_date = ev_slot(evidence, k, "cpu_path_validation")
        first = gates["implement"] or []
        if first:
            nxt = "Held: " + pre_catalog[first[0]]["unlock"]
        else:
            nxt = f"{wave}: build {', '.join(a[3:] for a in adapters) or 'loader card'}; prove '{skill_tests[0] if skill_tests else 'fixture'}'"
        skills[k] = {
            "catalog_commit": catalog["commit"],
            "family": r["families"][0], "families": r["families"],
            "profile_class": r["profile"], "priority": r["priority"], "readiness": r["readiness"],
            "wave": wave,
            "dependency_profile": profile,
            "runtime": rt,
            "dependencies": sorted({f"{d[0]} ({d[1]})" for d in r["dependencies"] if d[1] != "example_only"}),
            "compute": r["compute"], "hardware": r["hardware"],
            "network_services": sorted({n[0] for n in r["network"]}),
            "credentials": [{"name": c[0], "required": c[1]} for c in r["credentials"]],
            "adapters": adapters, "core_components": core, "frontend_components": fe,
            "backend_components_audit": r["backend_components"],
            "acceptance": {"suites": sorted(set(tests)), "skill_tests": skill_tests},
            "prerequisites": [p["id"] for p in pre],
            "prerequisite_gates": {p["id"]: p["gates"] for p in pre},
            "license_status": license_status(r, pre), "license_audit": r["license"]["commercial_use"],
            "asset_holds": ASSET_HOLDS.get(k),
            "statuses": statuses,
            "scientific_signoff": {"required": any(p["type"] == "specialist_review" for p in pre), "value": sign_ev is not None,
                                   "evidence": sign_ev, "date": sign_date,
                                   "reason": "separate from software validation (ADR-016 Q5)"},
            "gpu_validation": {"applicable": gpu_applicable, "cpu_path_supported": k in CPU_PATH,
                               "cpu_path": {"value": cpu_ev is not None, "evidence": cpu_ev, "date": cpu_date},
                               "gpu": {"value": gpu_ev is not None, "evidence": gpu_ev, "date": gpu_date,
                                       "unlock": "pre.gpu.cuda_host + at.gpu_run" if gpu_applicable else None}},
            "next_step": short(nxt, 180),
            "audit_source": {"audit": audit["audit_id"], "record": k, "skill_path": r["path"], "skill_sha256": r["sha256"],
                             "semantic_reviewed": r["semantic_reviewed"]},
        }
    images = collections.defaultdict(list)
    for k, s in skills.items():
        if s["runtime"]["image"]:
            images[s["runtime"]["image"]].append(k)
    profiles = {}
    for pid, (desc, py) in PROFILES.items():
        members = sorted(k for k, s in skills.items() if s["dependency_profile"] == pid)
        imgs = sorted({skills[k]["runtime"]["image"] for k in members if skills[k]["runtime"]["image"]})
        extra = []
        if pid == "prof.gpu-ml":
            extra = ["CPU-path image (arm64/amd64) and CUDA image (linux/amd64) built separately", "CUDA driver/toolkit/wheel compatibility matrix"]
        if any(skills[k]["runtime"]["arch"] == "linux/amd64" for k in members):
            extra.append("amd64-only members: build on amd64 builder or test under emulation on the arm64 host")
        if any(skills[k]["runtime"]["python"] in EOL_PY for k in members):
            extra.append("EOL Python member: security exception decision or upstream port before build")
        profiles[pid] = {"description": desc, "default_python": py, "skills": members,
                         "pythons": sorted({skills[k]["runtime"]["python"] for k in members if skills[k]["runtime"]["python"]}),
                         "archs": sorted({skills[k]["runtime"]["arch"] for k in members}),
                         "images": imgs, "build_scan": [] if pid == "prof.none" else BUILD_SCAN + extra}
    reg = {
        "schema_version": "2.0",
        "catalog": catalog,
        "inputs": {"data_files_sha256": data_sha, "audit_id": audit["audit_id"], "audit_platform_snapshot": audit["platform_snapshot"],
                   "mechanical_reviewed": len(recs), "semantic_reviewed": sum(r["semantic_reviewed"] for r in recs.values()),
                   "audit_scope": audit["scope"]},
        "platform": ledger["platform_verified"],
        "main_gates": ledger["main_gates"],
        "status_definitions": {
            "analyzed": "Audit record exists and passed the audit validator (mechanical: all; semantic: sampled subset only)",
            "implemented": "Merged code makes the capability callable: loader entry plus typed tool/adapter and/or profile image. Bundled SKILL.md alone does not count. Evidence: commit",
            "validated": "Software acceptance: the skill's representative scientific input/output checks passed on the real runtime. Evidence: test id + live evidence dir",
            "enabled": "Registered and switched on in the deployed configuration. Evidence: deployment config ref + image digest",
            "blocked": "Named prerequisite ids outside engineering; each prerequisite says which stage it gates",
            "scientific_signoff": "Separate from software validation: named qualified reviewer's recorded decision",
            "gpu_validation": "Separate from CPU-path tests: representative run on a reviewed CUDA host",
        },
        "waves": {k: {"title": t, "gates": g, "note": n} for k, (t, g, n) in WAVES.items()},
        "core_components": CORE, "adapters": ADAPTERS, "frontend_components": FRONTEND,
        "dependency_profiles": profiles, "images": {k: sorted(v) for k, v in sorted(images.items())},
        "acceptance_suites": TESTS,
        "prerequisite_types": {k: {"meaning": v[0], "default_unlock": v[1]} for k, v in PREREQ_TYPES.items()},
        "prerequisites": {k: pre_catalog[k] for k in sorted(pre_catalog)},
        "skills": skills,
    }
    validate(reg, catalog)
    return reg


def validate(reg, catalog):
    errs = []
    S = reg["skills"]
    if len(S) != 177 or catalog["skill_count"] != len(S):
        errs.append(f"expected 177 skills, got {len(S)}")
    for k, s in S.items():
        if not re.fullmatch(r"skills/%s/SKILL\.md" % re.escape(k), s["audit_source"]["skill_path"]) or not re.fullmatch(r"[0-9a-f]{64}", s["audit_source"]["skill_sha256"]):
            errs.append(f"{k}: source ref")
        if s["wave"] not in reg["waves"]:
            errs.append(f"{k}: wave")
        for field, cat in (("adapters", "adapters"), ("core_components", "core_components"), ("frontend_components", "frontend_components"),
                           ("prerequisites", "prerequisites")):
            errs += [f"{k}: {field} {x}" for x in s[field] if x not in reg[cat]]
        if s["dependency_profile"] not in reg["dependency_profiles"]:
            errs.append(f"{k}: profile")
        if s["runtime"]["image"] and s["runtime"]["image"] not in reg["images"]:
            errs.append(f"{k}: image")
        errs += [f"{k}: suite {x}" for x in s["acceptance"]["suites"] if x not in reg["acceptance_suites"]]
        st = s["statuses"]
        if list(st) != STATUS_KEYS:
            errs.append(f"{k}: status keys")
        if (st["enabled"]["value"] and not st["validated"]["value"]) or (st["validated"]["value"] and not st["implemented"]["value"]):
            errs.append(f"{k}: status ladder")
        if set(s["prerequisite_gates"]) != set(s["prerequisites"]) or any(g not in STAGES for g in s["prerequisite_gates"].values()):
            errs.append(f"{k}: prerequisite gates")
        if st["blocked"]["value"] != bool(s["prerequisites"]):
            errs.append(f"{k}: blocked flag")
        for name in STATUS_KEYS[:4]:
            v = st[name]
            if v["value"] != (v["evidence"] is not None) and name != "analyzed":
                errs.append(f"{k}: {name} value without evidence")
            if not v["value"] and not v["unlock"]:
                errs.append(f"{k}: {name} missing unlock")
        if not st["enabled"]["value"] and not (st["enabled"]["reason"] and st["enabled"]["unlock"]):
            errs.append(f"{k}: not enabled without reason/unlock")
        if s["scientific_signoff"]["required"] and st["enabled"]["value"] and not s["scientific_signoff"]["value"]:
            errs.append(f"{k}: enabled without required signoff")
        g = s["gpu_validation"]
        if g["applicable"] and g["gpu"]["value"] and g["gpu"]["evidence"] == g["cpu_path"]["evidence"]:
            errs.append(f"{k}: CPU-path evidence reused as GPU validation")
    for w, v in reg["waves"].items():
        errs += [f"wave {w}: gate {x}" for x in v["gates"]["merge"] if re.fullmatch(r"X\d+", x) and x not in reg["waves"]]
        errs += [f"wave {w}: gate {x}" for x in v["gates"]["merge"] if x.startswith("main:") and x.split()[0][5:] not in reg["main_gates"]]
    if any(x.startswith("main:B6") for x in reg["waves"]["X0"]["gates"]["merge"]):
        errs.append("X0 must not wait on B6 paper live acceptance")
    if errs:
        sys.exit("VALIDATION FAILED:\n" + "\n".join(errs))


def verify_platform():
    """Assert the recorded platform facts still hold in this checkout (not part of the outputs)."""
    manifest = json.loads((ROOT / "runtime/skills-manifest.json").read_text())
    adapter = (ROOT / "backend/src/scientist/runtime_adapter.py").read_text()
    names = sorted(s["name"] for s in manifest["skills"])
    problems = []
    if names != ["literature-review", "paper-lookup", "scientific-writing"]:
        problems.append(f"bundled skills changed: {names}")
    if '"enabled_toolsets": ["todo"]' not in adapter:
        problems.append("worker toolsets changed: re-check implemented statuses")
    if any(re.search(r"capability_registry|instruction_loader", p.read_text()) for p in (ROOT / "backend/src").rglob("*.py")):
        problems.append("registry/loader code present: record implemented evidence")
    if problems:
        sys.exit("PLATFORM FACTS CHANGED:\n" + "\n".join(problems))


# --------------------------------------------------------------------------- outputs
def yn(v):
    return "yes" if v else "no"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in row) + " |" for row in rows]
    return "\n".join(out)


def render_md(reg) -> str:
    S, C = reg["skills"], collections.Counter
    n = len(S)
    st = {k: sum(s["statuses"][k]["value"] for s in S.values()) for k in STATUS_KEYS}
    plat = reg["platform"]
    gpu_app = [s for s in S.values() if s["gpu_validation"]["applicable"]]
    sign_req = [s for s in S.values() if s["scientific_signoff"]["required"]]
    lines = [
        "# Scientific skills coverage (draft for owner review)", "",
        f"Generated by `tools/skills/build_registry.py` from tracked data (`tools/skills/data`, sha256 `{reg['inputs']['data_files_sha256'][:12]}`). "
        f"Catalog pin `{reg['catalog']['commit'][:12]}` ({reg['catalog']['skill_count']} skills); upstream main at check "
        f"`{reg['catalog']['upstream_main_at_check'][:12]}` ({'no delta' if reg['catalog']['upstream_main_at_check'] == reg['catalog']['commit'] else 'DELTA: re-audit'}). "
        f"Platform facts verified at `{plat['commit']}` on {plat['date']}.", "",
        "Internal developer document. Product UI must not show skill, agent or runtime names. Goal: all 177 as far as feasible (ADR-016).", "",
        "## Status definitions", "",
        *[f"- **{k}**: {v}" for k, v in reg["status_definitions"].items()], "",
        "## Verified platform facts", "", *[f"- {f}" for f in plat["facts"]], "",
        "## Totals per status", "",
        table(["Status", "Count", "Note"], [
            ["analyzed", st["analyzed"], f"all mechanically validated; {reg['inputs']['semantic_reviewed']} semantically reviewed"],
            ["implemented", st["implemented"], "no loader, registry or typed scientific tool yet"],
            ["validated (software)", st["validated"], "no capability acceptance run on the real runtime"],
            ["enabled", st["enabled"], "nothing registered in a deployment"],
            ["blocked", st["blocked"], "at least one named prerequisite id (gating implement, validate, GPU validation or enable)"],
            ["not blocked", n - st["blocked"], "engineering only; proceeds when its wave gate opens"],
            ["scientific signoff required / recorded", f"{len(sign_req)} / {sum(s['scientific_signoff']['value'] for s in sign_req)}", "separate from software validation"],
            ["GPU-applicable / CPU path available / GPU validated", f"{len(gpu_app)} / {sum(s['gpu_validation']['cpu_path_supported'] for s in gpu_app)} / {sum(s['gpu_validation']['gpu']['value'] for s in gpu_app)}", "CPU-path tests never count as GPU validation"],
        ]), "",
        "Blocked by stage gated (a skill counts once at its earliest gated stage):", "",
    ]
    order = {s: i for i, s in enumerate(STAGES)}
    earliest = C(STAGES[min(order[g] for g in s["prerequisite_gates"].values())] for s in S.values() if s["prerequisites"])
    lines.append(table(["Earliest gated stage", "Skills"], [[g, earliest[g]] for g in STAGES]))
    lines += ["", "## Totals per wave", ""]
    wc = C(s["wave"] for s in S.values())
    wb = C(s["wave"] for s in S.values() if s["statuses"]["blocked"]["value"])
    lines.append(table(["Wave", "Skills", "Blocked", "Scope", "Merge gates", "UI gates"],
                       [[w, wc[w], wb[w], v["title"], "; ".join(v["gates"]["merge"]), "; ".join(v["gates"]["ui"]) or "-"] for w, v in reg["waves"].items()]))
    lines += ["", "## Totals per family", ""]
    fc = C(s["family"] for s in S.values())
    fb = C(s["family"] for s in S.values() if s["statuses"]["blocked"]["value"])
    lines.append(table(["Family", "Skills", "Blocked", "Waves"],
                       [[FAM_SHORT[f], fc[f], fb[f], ", ".join(f"{w}:{c}" for w, c in sorted(C(s["wave"] for s in S.values() if s["family"] == f).items(), key=lambda x: int(x[0][1:])))]
                        for f in sorted(fc, key=lambda x: (-fc[x], x))]))
    lines += ["", "## Totals per prerequisite type", "", "A skill may count under several types.", ""]
    pc = C(t for s in S.values() for t in s["statuses"]["blocked"]["by"])
    lines.append(table(["Type", "Skills", "Meaning", "Unlock", "Examples"],
                       [[t, pc[t], reg["prerequisite_types"][t]["meaning"], reg["prerequisite_types"][t]["default_unlock"],
                         ", ".join(sorted(k for k, s in S.items() if t in s["statuses"]["blocked"]["by"])[:6])] for t in sorted(pc, key=lambda x: (-pc[x], x))]))
    lines += ["", "## Dependency profiles and images", "", "Each distinct image is built, scanned and license-inventoried separately; no single Python for all.", ""]
    lines.append(table(["Profile", "Skills", "Pythons", "Arch", "Images"],
                       [[p, len(v["skills"]), ", ".join(v["pythons"]) or "-", ", ".join(v["archs"]), len(v["images"])]
                        for p, v in sorted(reg["dependency_profiles"].items(), key=lambda x: -len(x[1]["skills"]))]))
    lines += ["", "## Per-skill coverage", "",
              "A = analyzed (S = semantic sample), I/V/E = implemented/validated/enabled, Sign = scientific signoff (req = required, not recorded), "
              "GPU = GPU validation (cpu = CPU path testable, gpu-only = no CPU path, - = n/a). Unlock lists what turns the next status on.", ""]
    rows = []
    for k, s in S.items():
        stt, rt = s["statuses"], s["runtime"]
        g = s["gpu_validation"]
        gpu = "-" if not g["applicable"] else ("yes" if g["gpu"]["value"] else ("cpu" if g["cpu_path_supported"] else "gpu-only"))
        sign = "-" if not s["scientific_signoff"]["required"] else ("yes" if s["scientific_signoff"]["value"] else "req")
        nxt = next((stt[x] for x in ("implemented", "validated", "enabled") if not stt[x]["value"]), None)
        unlock = nxt["unlock"] if nxt else "-"
        rows.append([f"`{k}`", FAM_SHORT[s["family"]], s["wave"], f"{rt['python'] or '-'}{' amd64' if rt['arch'] == 'linux/amd64' else ''}",
                     "S" if s["audit_source"]["semantic_reviewed"] else "yes", yn(stt["implemented"]["value"]), yn(stt["validated"]["value"]),
                     yn(stt["enabled"]["value"]), sign, gpu, ", ".join(stt["blocked"]["by"]) or "-",
                     short(stt["blocked"]["reason"] if stt["blocked"]["value"] else stt["implemented"]["reason"], 80), short(unlock, 120)])
    lines.append(table(["Skill", "Family", "Wave", "Py", "A", "I", "V", "E", "Sign", "GPU", "Blocked by", "Reason", "Unlock (next status)"], rows))
    lines += ["", "## Upstream watch items", "", "Not on upstream main; recorded only. Re-audit when a change reaches main.", ""]
    for w in reg["catalog"]["watch_branches"]:
        lines.append(f"- `{w['branch']}` @ {w['tip']}: {w['skill_count']} skills; adds {', '.join(w['adds']) or 'none'}; drops {w['drops']}. {w['action']}.")
    return "\n".join(lines) + "\n"


def plan_with_waves(reg) -> str:
    S = reg["skills"]
    lines = []
    for w in reg["waves"]:
        ks = [k for k, s in S.items() if s["wave"] == w]
        b = sum(S[k]["statuses"]["blocked"]["value"] for k in ks)
        lines.append(f"- **{w}** ({len(ks)}; blocked {b}): " + ", ".join(f"`{k}`" + ("†" if S[k]["statuses"]["blocked"]["value"] else "") for k in ks))
    text = OUT_PLAN.read_text(encoding="utf-8")
    start, end = "<!-- WAVE-LISTS -->", "<!-- /WAVE-LISTS -->"
    head, rest = text.split(start, 1)
    tail = rest.split(end, 1)[1] if end in rest else rest
    return head + start + "\n" + "\n".join(lines) + "\n" + end + tail


def main():
    reg = build()
    js = json.dumps(reg, ensure_ascii=False, indent=1) + "\n"
    md = render_md(reg)
    plan = plan_with_waves(reg)
    if "--verify-platform" in sys.argv:
        verify_platform()
    if "--check" in sys.argv:
        stale = [p.name for p, t in ((OUT_JSON, js), (OUT_MD, md), (OUT_PLAN, plan)) if not p.exists() or p.read_text(encoding="utf-8") != t]
        if stale:
            sys.exit("outputs differ from a fresh build: " + ", ".join(stale))
        print("check OK: 177 skills, outputs up to date")
        return
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(js, encoding="utf-8")
    OUT_MD.write_text(md, encoding="utf-8")
    OUT_PLAN.write_text(plan, encoding="utf-8")
    S = reg["skills"]
    print("skills", len(S), "waves", dict(sorted(collections.Counter(s["wave"] for s in S.values()).items(), key=lambda x: int(x[0][1:]))))
    print("blocked", sum(s["statuses"]["blocked"]["value"] for s in S.values()),
          dict(collections.Counter(t for s in S.values() for t in s["statuses"]["blocked"]["by"])))


if __name__ == "__main__":
    main()
