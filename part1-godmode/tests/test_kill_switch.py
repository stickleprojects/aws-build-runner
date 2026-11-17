#!/usr/bin/env python3
"""Offline tests for the kill switch (bootstrap/kill_switch.py), with a fake boto3. No AWS.

    python3 -I part1-godmode/tests/test_kill_switch.py
"""
import importlib
import json
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
failed = 0


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)}")
    failed += 0 if ok else 1


class Paginator:
    def __init__(self, client, method):
        self.client, self.method = client, method

    def paginate(self, **kwargs):
        yield getattr(self.client, self.method)(**kwargs)


class Fake:
    """One fake client per (service, region); every mutating call is recorded in CALLS."""
    def __init__(self, service, region):
        self.service, self.region = service, region

    def get_paginator(self, method):
        return Paginator(self, method)

    def __getattr__(self, name):
        def call(**kwargs):
            CALLS.append((self.service, self.region, name, kwargs))
            return WORLD.get((self.service, name), {})
        return call


CALLS = []
WORLD = {
    ("ec2", "describe_instances"): {"Reservations": [{"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]}]},
    ("ec2", "describe_spot_instance_requests"): {"SpotInstanceRequests": [{"SpotInstanceRequestId": "sir-1"}]},
    ("ec2", "describe_images"): {"Images": [{"ImageId": "ami-1", "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-1"}}]}]},
    ("ec2", "describe_volumes"): {"Volumes": [{"VolumeId": "vol-1"}]},
    ("ec2", "describe_addresses"): {"Addresses": [{"AllocationId": "eipalloc-free"},
                                                  {"AllocationId": "eipalloc-used", "AssociationId": "eipassoc-1"}]},
    ("ec2", "describe_vpc_endpoints"): {"VpcEndpoints": [{"VpcEndpointId": "vpce-1", "State": "available"},
                                                         {"VpcEndpointId": "vpce-2", "State": "deleting"}]},
    ("resourcegroupstaggingapi", "get_resources"): {"ResourceTagMappingList": [
        {"ResourceARN": "arn:aws:lambda:ap-southeast-2:1:function:build-runner-launcher"},
        {"ResourceARN": "arn:aws:lambda:ap-southeast-2:1:function:guardrail-kill-switch"}]},
}

sys.modules["boto3"] = types.SimpleNamespace(client=lambda service, region_name=None: Fake(service, region_name))
os.environ.update(REGIONS="ap-southeast-2,us-east-1", TERMINATE_INSTANCES="true", DELETE_RUNNER_AMIS="true",
                  THROTTLE_LAMBDAS="true", DELETE_DETACHED_VOLUMES="true", RELEASE_UNUSED_ADDRESSES="true",
                  DELETE_INTERFACE_ENDPOINTS="true")
sys.path.insert(0, str(HERE.parent / "bootstrap"))
ks = importlib.import_module("kill_switch")


def run(event):
    CALLS.clear()
    result = ks.handler(event, None)
    changes = [(svc, region, name, kw) for svc, region, name, kw in CALLS if not name.startswith(("describe_", "get_"))]
    return result, changes


def names(changes, region=None):
    return {name for _, r, name, _ in changes if region in (None, r)}


def sns(topic, message):
    return {"Records": [{"Sns": {"TopicArn": f"arn:aws:sns:ap-southeast-2:1:{topic}", "Message": message}}]}


# --- which mode
check("budget notification -> budget mode", ks.mode_of(sns("guardrail-budget-stop", "AWS Budget Notification")) == "budget")
check("alarm in ALARM -> fast mode", ks.mode_of(sns("guardrail-fast-stop", json.dumps({"NewStateValue": "ALARM"}))) == "fast")
check("alarm back to OK -> nothing", ks.mode_of(sns("guardrail-fast-stop", json.dumps({"NewStateValue": "OK"}))) is None)
check("unreadable alarm message -> fast (the safe side)", ks.mode_of(sns("guardrail-fast-stop", "not json")) == "fast")
check("by hand {} -> budget", ks.mode_of({}) == "budget")

# --- budget mode: everything
result, changes = run(sns("guardrail-budget-stop", "AWS Budget Notification"))
syd = names(changes, "ap-southeast-2")
for call in ("terminate_instances", "cancel_spot_instance_requests", "put_function_concurrency", "deregister_image",
             "delete_snapshot", "delete_volume", "release_address", "delete_vpc_endpoints"):
    check(f"budget: {call}", call in syd, sorted(syd))
check("budget: us-east-1 swept for EC2 too", "terminate_instances" in names(changes, "us-east-1"))
check("budget: Lambdas only throttled at home (Lambda is blocked elsewhere)",
      "put_function_concurrency" not in names(changes, "us-east-1"))
throttled = [kw["FunctionName"] for _, _, n, kw in changes if n == "put_function_concurrency"]
check("budget: our Lambda to 0, never the kill switch itself", throttled == ["build-runner-launcher"]
      and all(kw["ReservedConcurrentExecutions"] == 0 for _, _, n, kw in changes if n == "put_function_concurrency"), throttled)
released = [kw["AllocationId"] for _, _, n, kw in changes if n == "release_address"]
check("budget: only the unattached Elastic IP is released", set(released) == {"eipalloc-free"}, released)
ends = [kw["VpcEndpointIds"] for _, r, n, kw in changes if n == "delete_vpc_endpoints" and r == "ap-southeast-2"]
check("budget: an endpoint already deleting is left alone", ends == [["vpce-1"]], ends)

# --- fast mode: instances and Lambdas only
result, changes = run(sns("guardrail-fast-stop", json.dumps({"NewStateValue": "ALARM", "AlarmName": "x"})))
syd = names(changes, "ap-southeast-2")
check("fast: terminates and throttles", {"terminate_instances", "put_function_concurrency"} <= syd, sorted(syd))
check("fast: keeps AMIs, disks, IPs and endpoints",
      not syd & {"deregister_image", "delete_snapshot", "delete_volume", "release_address", "delete_vpc_endpoints"}, sorted(syd))
result, changes = run(sns("guardrail-fast-stop", json.dumps({"NewStateValue": "OK"})))
check("alarm back to OK: changes nothing", changes == [] and result["mode"] is None, changes)

# --- dry run
result, changes = run({"dry_run": True})
check("dry run: changes nothing", changes == [], changes)
check("dry run: still reports what it would do",
      result["regions"]["ap-southeast-2"]["instances"] == ["i-1", "i-2"]
      and result["regions"]["ap-southeast-2"]["lambdas_throttled"] == ["build-runner-launcher"], result)

# --- one failing step doesn't stop the rest
orig = WORLD[("ec2", "describe_images")]
WORLD[("ec2", "describe_images")] = None  # makes delete_amis raise
result, changes = run({})
WORLD[("ec2", "describe_images")] = orig
r = result["regions"]["ap-southeast-2"]
check("a failing step is logged and the others still run",
      r["errors"] and "delete_volume" in names(changes, "ap-southeast-2"), r)

# --- the flags turn steps off
os.environ["THROTTLE_LAMBDAS"] = "false"
result, changes = run({})
check("THROTTLE_LAMBDAS=false: no throttling", "put_function_concurrency" not in names(changes))
os.environ["THROTTLE_LAMBDAS"] = "true"

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
