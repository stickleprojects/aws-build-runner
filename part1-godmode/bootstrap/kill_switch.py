"""guardrail-kill-switch: stops what's spending when the guardrail-monthly budget hits its limit.

Triggered by the budget's 100 % ACTUAL notification (SNS topic guardrail-budget-stop). Godmode
deploys it (bootstrap-budget-stop.sh); Claude's policy denies it any access to this function.
  - terminates every instance in the account (pending, running, stopping, stopped)
  - cancels open spot requests
  - deletes the runner AMIs (tag resource-group=build-runner) and their snapshots
Invoke with {"dry_run": true} to see what it would do without doing it.
"""
import json
import os

import boto3

REGIONS = os.environ.get("REGIONS", "ap-southeast-2").split(",")
TERMINATE = os.environ.get("TERMINATE_INSTANCES") == "true"
DELETE_AMIS = os.environ.get("DELETE_RUNNER_AMIS") == "true"


def pages(client, method, key, **kwargs):
    for page in client.get_paginator(method).paginate(**kwargs):
        yield from page[key]


def stop_region(region, dry_run):
    ec2 = boto3.client("ec2", region_name=region)
    done = {"instances": [], "spot_requests": [], "amis": [], "snapshots": [], "errors": []}
    try:
        if TERMINATE:
            states = ["pending", "running", "stopping", "stopped"]
            ids = [i["InstanceId"]
                   for r in pages(ec2, "describe_instances", "Reservations",
                                  Filters=[{"Name": "instance-state-name", "Values": states}])
                   for i in r["Instances"]]
            if ids and not dry_run:
                for start in range(0, len(ids), 500):
                    ec2.terminate_instances(InstanceIds=ids[start:start + 500])
            done["instances"] = ids
            spot = [s["SpotInstanceRequestId"] for s in ec2.describe_spot_instance_requests(
                Filters=[{"Name": "state", "Values": ["open", "active"]}])["SpotInstanceRequests"]]
            if spot and not dry_run:
                ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=spot)
            done["spot_requests"] = spot
        if DELETE_AMIS:
            images = ec2.describe_images(Owners=["self"],
                                         Filters=[{"Name": "tag:resource-group", "Values": ["build-runner"]}])["Images"]
            for image in images:
                snaps = [m["Ebs"]["SnapshotId"] for m in image.get("BlockDeviceMappings", []) if "Ebs" in m]
                if not dry_run:
                    ec2.deregister_image(ImageId=image["ImageId"])
                    for snap in snaps:
                        ec2.delete_snapshot(SnapshotId=snap)
                done["amis"].append(image["ImageId"])
                done["snapshots"] += snaps
    except Exception as e:  # keep going in other regions; the log says what failed
        done["errors"].append(f"{type(e).__name__}: {e}")
    return done


def handler(event, context):
    dry_run = isinstance(event, dict) and bool(event.get("dry_run"))
    result = {"dry_run": dry_run, "regions": {r: stop_region(r, dry_run) for r in REGIONS}}
    print(json.dumps(result))
    return result
