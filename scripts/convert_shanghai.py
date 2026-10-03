"""Convert Shanghai per-patient xls/xlsx files to CSV, one at a time,
with per-file timeout and progress. Skips files already converted.

Usage:
    python scripts/convert_shanghai.py          # driver mode: convert all
    python scripts/convert_shanghai.py --single IN OUT   # worker: convert one

Design: driver spawns itself as a subprocess per file so we can enforce a hard
30-second timeout on the pandas.read_excel call (signal.alarm is Unix-only, and
threading a timeout won't interrupt a native xlrd read on Windows).
"""
import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "diabetes_datasets"
OUT_ROOT = ROOT / "data" / "interim" / "shanghai"
GROUPS = ("Shanghai_T1DM", "Shanghai_T2DM")
TIMEOUT_S = 30


def convert_one(in_path: str, out_path: str) -> None:
    """Worker: read one Excel file and write CSV. Runs in a subprocess."""
    import pandas as pd
    engine = "openpyxl" if in_path.lower().endswith(".xlsx") else "xlrd"
    df = pd.read_excel(in_path, engine=engine)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8")


def drive() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    all_files: list[tuple[str, Path]] = []
    for g in GROUPS:
        for p in sorted((DATA / g).glob("*.xls*")):
            all_files.append((g, p))

    print(f"[driver] {len(all_files)} files across {len(GROUPS)} groups", flush=True)
    failures: list[dict] = []
    n_ok = n_skip = n_fail = 0

    for i, (group, src) in enumerate(all_files, 1):
        out_dir = OUT_ROOT / group
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / (src.stem + ".csv")
        tag = f"[{i:3d}/{len(all_files)}] {group}/{src.name}"

        if dst.exists() and dst.stat().st_size > 0:
            print(f"{tag}  SKIP (exists)", flush=True)
            n_skip += 1
            continue

        t0 = time.perf_counter()
        try:
            subprocess.run(
                [sys.executable, __file__, "--single", str(src), str(dst)],
                check=True,
                timeout=TIMEOUT_S,
                capture_output=True,
                text=True,
            )
            dt_ms = (time.perf_counter() - t0) * 1000
            print(f"{tag}  OK   {dt_ms:7.0f} ms", flush=True)
            n_ok += 1
        except subprocess.TimeoutExpired:
            dt_ms = (time.perf_counter() - t0) * 1000
            print(f"{tag}  TIMEOUT {dt_ms:7.0f} ms", flush=True)
            failures.append(dict(file=str(src), reason="timeout"))
            n_fail += 1
            if dst.exists():
                try: dst.unlink()
                except OSError: pass
        except subprocess.CalledProcessError as e:
            dt_ms = (time.perf_counter() - t0) * 1000
            err = (e.stderr or "").strip().splitlines()[-1:] if e.stderr else [""]
            print(f"{tag}  FAIL {dt_ms:7.0f} ms  -> {err[0] if err else ''}", flush=True)
            failures.append(dict(file=str(src), reason=(e.stderr or "").strip()))
            n_fail += 1
            if dst.exists():
                try: dst.unlink()
                except OSError: pass

    print(f"\n[driver] done. ok={n_ok} skipped={n_skip} failed={n_fail}", flush=True)
    if failures:
        print("[driver] failures:", flush=True)
        for f in failures:
            print(f"  - {f['file']}", flush=True)
            for line in f["reason"].splitlines()[-3:]:
                print(f"      {line}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--single":
        convert_one(sys.argv[2], sys.argv[3])
        sys.exit(0)
    drive()
