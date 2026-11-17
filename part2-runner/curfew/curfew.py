#!/usr/bin/env python3
"""The runner curfew, by hand: see it, extend it, stop now, or resume. Used by the curfew workflow
(.github/workflows/curfew.yml, as /claude/build-runner-curfew) and the runner-curfew skill (as Claude's user).

    curfew.py status            when runners stop or start next, and why
    curfew.py extend 1|2        push tonight's stop back by 1 or 2 hours (or restart for that long, once stopped)
    curfew.py stop              stop now: the launcher terminates every runner within 5 minutes, until 07:00
    curfew.py resume            clear any stop or extension (back to the plain quiet hours)
    curfew.py notice            JSON {"due": true, "stop_at": ..., "stop_local": ...} when a notice should go out

The quiet hours come from part2-runner/stack/config.json (Curfew); the rules are stack/lambda/curfew.py, the same
code the launcher runs. Overrides are the SSM parameter /build/runner/curfew.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "stack" / "lambda"))  # python3 -I leaves it out
import curfew  # noqa: E402

REGION = "ap-southeast-2"
CFG = {**curfew.DEFAULT, **json.loads((HERE.parent / "stack" / "config.json").read_text()).get("Curfew", {})}


def aws(*args: str) -> str:
    return subprocess.run(["aws", *args, "--region", REGION, "--output", "text"], check=True,
                          capture_output=True, text=True).stdout.strip()


def get() -> dict:
    try:
        return curfew.parse(aws("ssm", "get-parameter", "--name", curfew.PARAMETER, "--query", "Parameter.Value"))
    except subprocess.CalledProcessError as e:
        if "ParameterNotFound" in e.stderr:
            return {}
        raise


def put(override: dict) -> None:
    aws("ssm", "put-parameter", "--name", curfew.PARAMETER, "--type", "String", "--overwrite",
        "--value", json.dumps({k: int(v) for k, v in override.items()}),
        "--description", "Runner curfew overrides (extend_until / stop_until, epoch seconds); see part2-runner/curfew")


def describe(now: float, s: dict) -> str:
    when = curfew.local(s["until"], CFG)
    if s["active"]:
        return (f"Runners are OFF ({s['reason']}) until {when} Sydney. Queued jobs wait and start then. "
                "`/extend 1h` or `/extend 2h` turns them back on for that long.")
    if s["reason"] == "extended":
        if not s.get("stop_at"):
            return f"Runners are ON (extended) through to the morning ({when} Sydney): no stop tonight."
        return f"Runners are ON (extended) until {when} Sydney, then off until {CFG['end']}."
    return f"Runners are ON; they stop at {when} Sydney (quiet hours {CFG['start']}-{CFG['end']})."


def main(argv: list[str]) -> int:
    now = time.time()
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        print(describe(now, curfew.state(now, get(), CFG)))
    elif cmd == "extend":
        hours = int((argv[2] if len(argv) > 2 else "").rstrip("h") or 0)
        put(curfew.extend(now, get(), hours, CFG))
        print(describe(now, curfew.state(now, get(), CFG)))
    elif cmd == "stop":
        put(curfew.stop(now, CFG))
        print(describe(now, curfew.state(now, get(), CFG)) + " (The launcher acts within 5 minutes.)")
    elif cmd == "resume":
        put({})
        print(describe(now, curfew.state(now, {}, CFG)))
    elif cmd == "notice":
        due = curfew.notice_due(now, get(), CFG)
        print(json.dumps({"due": bool(due), **({"stop_at": int(due["stop_at"]),
                                                "stop_local": curfew.local(due["stop_at"], CFG),
                                                "minutes": round((due["stop_at"] - now) / 60)} if due else {})}))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except ValueError as e:
        print(f"curfew: {e}", file=sys.stderr)
        sys.exit(2)
