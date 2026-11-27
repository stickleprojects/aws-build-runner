#!/usr/bin/env python3
"""Offline tests for the consumer pools: lambda/pools.py's checks and the launcher's load_pools (#54, phase 1).

    python3 -I part2-runner/stack/test_pools.py
"""
import base64
import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lambda"))  # python3 -I leaves it out
import pools    # noqa: E402
import runner   # noqa: E402

failed = 0


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)}")
    failed += 0 if ok else 1


B = {"instance_types": ["t3.*", "t3a.*", "t4g.*", "m*.large", "m*.xlarge", "c*.large", "c*.xlarge"],
     "instance_sizes": ["micro", "small", "medium", "large", "xlarge"],
     "max_runners": 4, "max_instance_minutes": 90, "max_idle_minutes": 20}
# spellbound3d's agreed v1 file (their tools/runner-image/pools.json, 2026-11-18).
GOOD = {"version": 1,
        "pools": {"aws-light": {"class": "light", "spot": ["c7i.large", "t3.large"], "on_demand": ["c7i.large"]},
                  "aws-heavy": {"class": "heavy", "spot": ["c7i.xlarge", "c6i.xlarge", "m7i.xlarge"],
                                "on_demand": ["c7i.xlarge"]}},
        "limits": {"max_runners": 4, "max_instance_minutes": 90, "idle_minutes": 20}}


def refused(doc, needle):
    try:
        pools.validate(doc, B)
    except pools.Invalid as e:
        return needle in str(e)
    return False


def edit(fn):
    d = copy.deepcopy(GOOD)
    fn(d)
    return d


out = pools.validate(GOOD, B)
check("the agreed file passes", sorted(out["pools"]) == ["aws-heavy", "aws-light"] and out["limits"]["max_runners"] == 4)
check("limits left out take the boundary's",
      pools.effective(pools.validate(edit(lambda d: d.pop("limits")), B), B)
      == {"max_runners": 4, "max_instance_minutes": 90, "idle_minutes": 20})
check("stricter limits are kept", pools.effective(pools.validate(edit(lambda d: d["limits"].update(max_runners=2)), B), B)["max_runners"] == 2)
config = json.loads((HERE / "config.json").read_text())
check("today's config.json Pools pass as a v1 file", pools.validate({"version": 1, "pools": config["Pools"]}, B)["pools"].keys()
      == {"aws-light", "aws-heavy", "aws-spellbound3d-light", "aws-spellbound3d-heavy"})

check("version 2 is refused", refused(edit(lambda d: d.update(version=2)), "version"))
check("true isn't version 1", refused(edit(lambda d: d.update(version=True)), "version"))
check("an unknown top-level key is refused", refused(edit(lambda d: d.update(curfew={})), "unknown key 'curfew'"))
check("an unknown pool key is refused", refused(edit(lambda d: d["pools"]["aws-light"].update(gpu=True)), "unknown key 'gpu'"))
check("an unknown limit is refused", refused(edit(lambda d: d["limits"].update(max_disk_gib=100)), "unknown key"))
check("a label without the aws- prefix is refused", refused(edit(lambda d: d["pools"].update(heavy=d["pools"]["aws-heavy"])), "labels must match"))
check("an unknown class is refused", refused(edit(lambda d: d["pools"]["aws-light"].update(**{"class": "gpu"})), "class 'gpu'"))
check("an empty spot list is refused", refused(edit(lambda d: d["pools"]["aws-light"].update(spot=[])), "at least 1"))
check("on_demand [] (spot only) is fine", pools.validate(edit(lambda d: d["pools"]["aws-light"].update(on_demand=[])), B) is not None)
check("a type twice is refused", refused(edit(lambda d: d["pools"]["aws-light"].update(spot=["c7i.large", "c7i.large"])), "twice"))
check("2xlarge (8 vCPU) is refused", refused(edit(lambda d: d["pools"]["aws-heavy"]["spot"].append("c7i.2xlarge")), "bigger than"))
check("Graviton t4g is refused although the policy allows t4g.*",
      refused(edit(lambda d: d["pools"]["aws-light"]["spot"].append("t4g.large")), "Graviton"))
for g in ("c7g.large", "c7gn.xlarge", "m6gd.large", "c8g.xlarge"):
    check(f"Graviton {g} is refused", refused(edit(lambda d: d["pools"]["aws-light"]["spot"].append(g)), "Graviton"))
for ok_type in ("c7i.large", "c6a.xlarge", "m7i-flex.large", "t3a.large", "c6id.xlarge"):
    check(f"x86 {ok_type} passes", pools.validate(edit(lambda d: d["pools"]["aws-light"].update(spot=[ok_type])), B) is not None)
check("an unapproved family (r5) is refused", refused(edit(lambda d: d["pools"]["aws-light"]["spot"].append("r5.large")), "approved"))
check("a GPU type is refused", refused(edit(lambda d: d["pools"]["aws-heavy"]["spot"].append("g5.xlarge")), "approved"))
check("max_runners over 4 is refused", refused(edit(lambda d: d["limits"].update(max_runners=5)), "over the boundary"))
check("max_instance_minutes over 90 is refused", refused(edit(lambda d: d["limits"].update(max_instance_minutes=120)), "over"))
check("idle_minutes over 20 is refused", refused(edit(lambda d: d["limits"].update(idle_minutes=30)), "over"))
check("a zero limit is refused", refused(edit(lambda d: d["limits"].update(max_runners=0)), "positive"))
try:
    pools.validate(edit(lambda d: (d.update(version=3), d["pools"]["aws-light"].update(spot=[]))), B)
    check("every reason is reported at once", False)
except pools.Invalid as e:
    check("every reason is reported at once", len(e.reasons) >= 2, e.reasons)


# --- the launcher's load_pools: consumer copy, last good, config.json
class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class SSM:
    def __init__(self):
        self.p, self.puts = {}, []

    def get_parameter(self, Name, **k):
        if Name not in self.p:
            raise ClientError("ParameterNotFound")
        return {"Parameter": {"Value": self.p[Name]}}

    def put_parameter(self, Name, Value, **k):
        self.p[Name] = Value
        self.puts.append(Name)


class GH:
    def __init__(self, doc=None, status=200, fail=False):
        self.doc, self.status, self.fail, self.calls = doc, status, fail, 0

    def call(self, method, path, body=None, ok=(200,)):
        self.calls += 1
        if self.fail:
            raise RuntimeError("GitHub GET: HTTP 502")
        if self.status == 404:
            return 404, {}
        return 200, {"content": base64.b64encode(json.dumps(self.doc).encode()).decode(), "sha": "abc123def4567890"}


runner.BOUNDARY = B
runner.log = lambda *a, **k: None
ssm = SSM()
runner._pools_cache.update(at=0.0, checked=None)
got = runner.load_pools(GH(GOOD), 1000.0, ssm)
check("load: a good consumer copy is used", sorted(got["pools"]) == ["aws-heavy", "aws-light"])
check("load: ...saved as the last good copy", json.loads(ssm.p[runner.POOLS_LAST_GOOD]) == got)
check("load: ...and the status says ok", json.loads(ssm.p[runner.POOLS_STATUS])["state"] == "ok")
st = json.loads(ssm.p[runner.POOLS_STATUS])
check("load: the status records the blob sha read and when (2026-11-28)",
      st.get("sha") == "abc123def456" and st.get("read_at") == 1000, st)
gh = GH(GOOD)
runner.load_pools(gh, 1000.0 + 60, ssm)
check("load: within 10 minutes it isn't read again", gh.calls == 0)
bad = edit(lambda d: d["pools"]["aws-heavy"]["spot"].append("c7i.4xlarge"))
got = runner.load_pools(GH(bad), 1000.0 + 700, ssm)
check("load: a refused copy keeps the last good one", "c7i.4xlarge" not in json.dumps(got) and "aws-heavy" in got["pools"])
st = json.loads(ssm.p[runner.POOLS_STATUS])
check("load: ...and the status says refused, with the reason", st["state"] == "refused" and "4xlarge" in st["reason"], st)
since = json.loads(ssm.p[runner.POOLS_STATUS])["since"]
runner._pools_cache.update(at=0.0)
runner.load_pools(GH(bad), 5000.0, ssm)
st = json.loads(ssm.p[runner.POOLS_STATUS])
check("load: the same refusal keeps its 'since', but read_at moves on", st["since"] == since and st["read_at"] == 5000, st)
runner._pools_cache.update(at=0.0)
got = runner.load_pools(GH(fail=True), 9000.0, ssm)
check("load: GitHub unreachable keeps the last good copy", "aws-light" in got["pools"]
      and json.loads(ssm.p[runner.POOLS_STATUS])["state"] == "unreachable")
fresh = SSM()
runner._pools_cache.update(at=0.0, checked=None)
got = runner.load_pools(GH(status=404), 1000.0, fresh)
check("load: no consumer file and no last good: config.json's pools", sorted(got["pools"]) == sorted(runner.POOLS)
      and json.loads(fresh.p[runner.POOLS_STATUS])["state"] == "missing")
check("load: ...config.json's pools aren't saved as the consumer's last good", runner.POOLS_LAST_GOOD not in fresh.p)

before = (runner.MAX_RUNNERS, runner.MAX_AGE, runner.IDLE_MAX)
runner.use_pools(pools.validate(edit(lambda d: d["limits"].update(max_runners=2, idle_minutes=10)), B))
check("use: stricter consumer limits take effect", (runner.MAX_RUNNERS, runner.IDLE_MAX) == (2, 600))
runner.use_pools(pools.validate(GOOD, B))
check("use: and back", (runner.MAX_RUNNERS, runner.MAX_AGE, runner.IDLE_MAX) == before, (runner.MAX_RUNNERS, runner.MAX_AGE))

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
