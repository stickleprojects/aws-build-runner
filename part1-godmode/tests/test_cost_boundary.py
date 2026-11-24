"""Offline: break each cost setting in turn and check cost_boundary_holds catches it, then put the file back.
Run from the repo root: python3 -I part1-godmode/tests/test_cost_boundary.py (exits 1 if any breach is missed)."""
import subprocess
from pathlib import Path

CASES = [
    ("part2-runner/stack/config.json", '"MaxRunners": 4', '"MaxRunners": 5', "MaxRunners 5"),
    ("part2-runner/stack/config.json", '"c7i.xlarge",\n        "c6i.xlarge"', '"c7i.2xlarge",\n        "c6i.xlarge"', "c7i.2xlarge"),
    ("part2-runner/stack/config.json", '"start": "22:00"', '"start": "23:30"', "curfew is"),
    ("part2-runner/stack/config.json", '"MaxAgeMinutes": 90', '"MaxAgeMinutes": 240', "MaxAgeMinutes 240"),
    ("part1-godmode/policy.yaml", "  budget_usd: 29\n", "  budget_usd: 35\n", "budget_usd 35"),
    ("part2-runner/ami/files/build-runner-boot.sh", "EGRESS_KBPS=2500", "EGRESS_KBPS=9000", "EGRESS_KBPS"),
    (".github/workflows/usage.yml", "jobs:", "# aws ce get-cost-and-usage\njobs:", "calls Cost Explorer"),
    ("part2-runner/stack/template.yaml", "VolumeSize: 30", "VolumeSize: 100", "100 GiB"),
    (".github/workflows/ami.yml", "prune-amis.sh 3", "prune-amis.sh 10", "keeps 10"),
]
failed_to_catch = 0
for path, old, new, expect in CASES:
    f = Path(path)
    original = f.read_text()
    assert original.count(old) >= 1, (path, old)
    f.write_text(original.replace(old, new, 1))
    try:
        out = subprocess.run(["python3", "-I", "part1-godmode/tests/regressions.py", "cost_boundary_holds"],
                             capture_output=True, text=True).stdout
    finally:
        f.write_text(original)
    caught = "FAIL cost_boundary_holds" in out and expect in out
    failed_to_catch += not caught
    print(("caught " if caught else "MISSED ") + f"{path}: {new.strip()[:40]}")
print("all caught" if not failed_to_catch else f"{failed_to_catch} missed")
raise SystemExit(1 if failed_to_catch else 0)
