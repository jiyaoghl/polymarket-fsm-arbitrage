from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path.cwd().resolve()


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _aiconfig_dir() -> Path:
    d = REPO_ROOT / ".aiconfig"
    d.mkdir(parents=True, exist_ok=True)
    (d / "history").mkdir(parents=True, exist_ok=True)
    return d


def _classify_path(p: Path) -> str:
    s = p.as_posix().lower()
    if "/.venv/" in s or "/env/" in s:
        return "venv"
    if "/__pycache__/" in s or "/.pytest_cache/" in s:
        return "cache"
    if s.startswith(".git/") or "/.git/" in s:
        return "vcs"
    if s.startswith("docs/"):
        return "docs"
    if s.startswith("tests/"):
        return "tests"
    if s.startswith("scripts/"):
        return "scripts"
    if s.startswith("configs/"):
        return "configs"
    if s.startswith("src/"):
        return "code"
    if s.startswith("data/"):
        return "data"
    if s.startswith("logs/"):
        return "logs"
    if s.startswith("tmp/"):
        return "tmp"
    if s.startswith("docker/"):
        return "docker"
    if p.suffix in {".md", ".mdc"}:
        return "docs"
    if p.suffix in {".json", ".yaml", ".yml", ".toml", ".ini"}:
        return "config"
    if p.suffix in {".bat", ".ps1", ".sh"}:
        return "script"
    return "other"


def analyze() -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for p in REPO_ROOT.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(REPO_ROOT)
        kind = _classify_path(rel)
        if kind in {"venv", "cache", "vcs"}:
            continue
        rows.append({"path": rel.as_posix(), "kind": kind, "bytes": p.stat().st_size})
    summary: Dict[str, int] = {}
    for r in rows:
        summary[r["kind"]] = summary.get(r["kind"], 0) + 1
    return {"root": str(REPO_ROOT), "files": rows, "summary": summary}


def _run(cmd: List[str]) -> Dict[str, Any]:
    p = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    return {
        "cmd": cmd,
        "returncode": p.returncode,
        "stdout": p.stdout,
        "stderr": p.stderr,
    }


def validate() -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    results.append(_run([sys.executable, "-m", "compileall", "src"]))
    results.append(_run([sys.executable, "-m", "pytest", "-q"]))
    ok = all(r["returncode"] == 0 for r in results)
    return {"ok": ok, "results": results}


def write_manifest(payload: Dict[str, Any]) -> Path:
    out = _aiconfig_dir() / "manifest.yaml"
    # 简单 YAML（避免引入 PyYAML 依赖）
    lines = [
        f"structure_version: {payload.get('structure_version', 1)}",
        f"generated_at: {payload.get('generated_at')}",
        f"root: {payload.get('root')}",
        "artifacts:",
        f"  structure_diff: {payload.get('structure_diff')}",
        f"  refactor_report: {payload.get('refactor_report')}",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ai_refactor", description="Project structure analyzer/refactor/validator")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--refactor", action="store_true", help="reserved (this repo was already refactored)")
    p.add_argument("--validate", action="store_true")
    args = p.parse_args(argv)

    ts = _ts()
    history_dir = _aiconfig_dir() / "history" / ts
    history_dir.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {"ts": ts, "root": str(REPO_ROOT), "steps": []}

    if args.analyze:
        a = analyze()
        (history_dir / "analyze.json").write_text(json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")
        report["steps"].append({"analyze": a["summary"]})

    if args.refactor:
        report["steps"].append({"refactor": "noop (already applied in-repo)"})

    if args.validate:
        v = validate()
        (history_dir / "validate.json").write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
        report["steps"].append({"validate_ok": v["ok"]})
        if not v["ok"]:
            # 让 CI 明确失败
            (history_dir / "FAILED").write_text("validation failed\n", encoding="utf-8")

    (history_dir / "run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    write_manifest(
        {
            "structure_version": 1,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "root": str(REPO_ROOT),
            "structure_diff": "structure_diff.json",
            "refactor_report": "refactor_report.md",
        }
    )
    print(str(history_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

