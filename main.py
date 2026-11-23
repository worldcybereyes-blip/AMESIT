"""AMESIT command-line orchestrator.

Usage (from the AMESIT folder, inside the virtual environment):

    python main.py generate-data            # create data/sample_io_campaign.json
    python main.py run                      # full pipeline on the configured input
    python main.py run --input my.csv --open
    python main.py selftest                 # run + assert against synthetic ground truth

The default sub-command is ``run``.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import time
import warnings
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 1))  # silences a joblib warning on Windows

for _s in (sys.stdout, sys.stderr):  # legacy Windows code pages must never crash a run
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")

log = logging.getLogger("amesit")


def load_config(path: Path) -> Dict[str, Any]:
    import yaml

    if not path.exists():
        log.warning("Config %s not found; using built-in defaults", path)
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (ROOT / p)


def setup_logging(outdir: Path, verbose: bool) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(outdir / "amesit.log", mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    for noisy in ("matplotlib", "PIL", "fontTools", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def banner(cfg: Dict[str, Any]) -> None:
    from src import __version__

    print("=" * 74)
    print(f"  AMESIT {__version__} - The Information Operations Engine")
    print("  Coordinated-behaviour detection | NLP | relationship mapping | reporting")
    print("=" * 74)


class Stages:
    """Tiny stage timer that prints a progress line per stage."""

    def __init__(self) -> None:
        self.t: Dict[str, float] = {}

    def run(self, name: str, fn):
        print(f"\n[{len(self.t) + 1}] {name} ...")
        t0 = time.perf_counter()
        out = fn()
        self.t[name] = time.perf_counter() - t0
        print(f"    done in {self.t[name]:.1f}s")
        return out


# ---------------------------------------------------------------------------
def cmd_generate(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    from src.osint_collector import generate_sample_dataset

    out = resolve(args.out or cfg.get("paths", {}).get("input", "data/sample_io_campaign.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    seed = int(args.seed if args.seed is not None else cfg.get("project", {}).get("random_seed", 1337))
    info = generate_sample_dataset(out, seed=seed)
    print(f"Synthetic dataset written: {out}")
    print("  " + ", ".join(f"{k}={v}" for k, v in info.items() if not isinstance(v, (dict, list))))
    return 0


def run_pipeline(args: argparse.Namespace, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from src.content_analyzer import ContentAnalyzer
    from src.nlp_engine import NLPEngine
    from src.osint_collector import OSINTCollector
    from src.relationship_mapper import RelationshipMapper
    from src.report_generator import ReportGenerator

    paths_cfg = cfg.get("paths", {})
    inp = resolve(args.input or paths_cfg.get("input", "data/sample_io_campaign.json"))
    outdir = resolve(args.output or paths_cfg.get("output_dir", "output"))
    docs = resolve(paths_cfg.get("docs_dir", "docs"))
    setup_logging(outdir, args.verbose)
    banner(cfg)
    if not inp.exists():
        if args.input:
            print(f"ERROR: input not found: {inp}")
            return None
        print(f"Input {inp} not found - generating the synthetic sample first.")
        from src.osint_collector import generate_sample_dataset

        inp.parent.mkdir(parents=True, exist_ok=True)
        generate_sample_dataset(inp, seed=int(cfg.get("project", {}).get("random_seed", 1337)))
    print(f"Input : {inp}\nOutput: {outdir}")
    st = Stages()

    ds = st.run("OSINT collection and normalisation", lambda: OSINTCollector(cfg).load(inp))
    print("    " + ", ".join(f"{k}={v}" for k, v in ds.summary().items() if k in ("posts", "accounts", "registered_domains", "iocs")))
    nlp = st.run("NLP: similarity, narrative clustering, sentiment, entities", lambda: NLPEngine(cfg).fit(ds.posts))
    print(f"    backends: {nlp.backend_info}")
    res = st.run("Content analysis: bursts, copy-paste, coordination, infrastructure",
                 lambda: ContentAnalyzer(cfg).run(ds, nlp))
    print("    " + ", ".join(f"{k}={v}" for k, v in res.headline().items()))

    gpaths: Dict[str, str] = {}
    gmetrics: Dict[str, Any] = {}
    if not args.skip_graph:
        def _graph():
            rm = RelationshipMapper(cfg)
            rm.build(ds, nlp, res)
            p = rm.export(outdir / "graphs", res)
            return p, rm.metrics

        gpaths, gmetrics = st.run("Relationship mapping and graph export", _graph)
        print(f"    graph: {gmetrics.get('nodes')} nodes, {gmetrics.get('edges')} edges")

    rg = ReportGenerator(cfg)
    rpaths: Dict[str, str] = {}
    if args.skip_pdf:
        print("    (--skip-pdf: PDF steps are skipped; Markdown/HTML are still produced)")
    rpaths = st.run("Reporting: dossier, dashboard, IOC exports",
                    lambda: rg.build(ds, nlp, res, outdir, gpaths, gmetrics, st.t, str(args.config)))
    if cfg.get("report", {}).get("regenerate_project_report", True) and not args.skip_pdf:
        try:
            pr = st.run("Project report PDF (docs/)",
                        lambda: rg.build_project_report(docs / "AMESIT_Project_Report.pdf", outdir / "figures"))
            rpaths["project_report"] = str(pr)
        except Exception as exc:  # never let the optional report kill a finished analysis
            log.warning("Project report skipped: %s", exc)

    summary = {"input": str(inp), "output": str(outdir), "headline": res.headline(), "dataset": ds.summary(),
               "evaluation": res.evaluation, "graph": gmetrics, "timings_s": {k: round(v, 2) for k, v in st.t.items()},
               "outputs": {**gpaths, **rpaths}}
    (outdir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 74)
    print("  RESULTS")
    print("=" * 74)
    for k, v in res.headline().items():
        print(f"  {k:<28} {v}")
    ev = res.evaluation
    if ev:
        a, d = ev["accounts"], ev["domains"]
        print(f"  synthetic ground truth      accounts P={a['precision']:.2f} R={a['recall']:.2f} F1={a['f1']:.2f}; "
              f"domains P={d['precision']:.2f} R={d['recall']:.2f}; waves {ev['waves_detected']}")
    print(f"\n  Dashboard : {outdir / 'dashboard.html'}")
    print(f"  Dossier   : {outdir / 'AMESIT_Intelligence_Dossier.pdf'}")
    print(f"  Network   : {outdir / 'graphs' / 'amesit_network.html'}")
    print(f"  IOCs      : {outdir / 'iocs'}")
    print(f"  Total time: {sum(st.t.values()):.1f}s")
    summary["_dashboard"] = outdir / "dashboard.html"
    return summary


def open_path(p: Path) -> None:
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(p))  # type: ignore[attr-defined]  # Windows
        else:
            webbrowser.open(p.as_uri())
    except Exception:
        try:
            webbrowser.open(p.as_uri())
        except Exception:
            print(f"Open this file manually: {p}")


def cmd_run(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    summary = run_pipeline(args, cfg)
    if summary is None:
        return 2
    if args.open:
        open_path(summary["_dashboard"])
    return 0


def cmd_selftest(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    args.skip_pdf = False
    args.input = args.input or None
    summary = run_pipeline(args, cfg)
    if summary is None:
        return 2
    ev = summary["evaluation"]
    if not ev:
        print("SELFTEST: no ground truth in input; nothing to assert.")
        return 0
    checks = {
        "account precision >= 0.90": ev["accounts"]["precision"] >= 0.90,
        "account recall >= 0.75": ev["accounts"]["recall"] >= 0.75,
        "all campaign domains found": ev["domains"]["recall"] >= 1.0 and ev["domains"]["precision"] >= 0.90,
        "all waves detected": ev["waves_detected"].split("/")[0] == ev["waves_detected"].split("/")[1],
        "organic burst not flagged": bool((ev.get("organic_burst_control") or {}).get("classified_organic")),
        "no organic accounts flagged": len(ev["organic_accounts_flagged"]) == 0,
    }
    outdir = Path(summary["output"])
    for f in ("dashboard.html", "AMESIT_Intelligence_Dossier.pdf", "AMESIT_Intelligence_Dossier.md", "iocs/iocs_stix.json",
              "graphs/amesit_network.html", "graphs/full_network.png"):
        checks[f"output exists: {f}"] = (outdir / f).exists() and (outdir / f).stat().st_size > 0
    print("\nSELFTEST")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="amesit", description="AMESIT - The Information Operations Engine")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml (default: config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--input", help="input file/folder (.json .jsonl .csv)")
        sp.add_argument("--output", help="output directory")
        sp.add_argument("--skip-graph", action="store_true", help="skip relationship graph export")
        sp.add_argument("--skip-pdf", action="store_true", help="skip PDF generation")

    g = sub.add_parser("generate-data", help="write the synthetic sample dataset")
    g.add_argument("--out", help="output path")
    g.add_argument("--seed", type=int)
    r = sub.add_parser("run", help="run the full pipeline (default)")
    common(r)
    r.add_argument("--open", action="store_true", help="open the dashboard when finished")
    r.add_argument("--no-eval", action="store_true", help="(reserved) ignore embedded ground truth")
    t = sub.add_parser("selftest", help="run the pipeline and assert against synthetic ground truth")
    common(t)
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args(["run"] + (argv if argv is not None else sys.argv[1:]))
    cfg_path = resolve(args.config)
    cfg = load_config(cfg_path)
    args.config = cfg_path
    if args.cmd == "generate-data":
        return cmd_generate(args, cfg)
    if args.cmd == "selftest":
        return cmd_selftest(args, cfg)
    return cmd_run(args, cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
