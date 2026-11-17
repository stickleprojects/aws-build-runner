"""guardrail-kill-switch: stops what's spending. Godmode deploys it (bootstrap-budget-stop.sh); Claude's
policy denies it any access to this function.

Two triggers, two modes (#30):
  budget  the guardrail-monthly budget's 100 % ACTUAL notification (SNS topic guardrail-budget-stop).
          Everything the flags turn on:
            - terminates every instance in the account (pending, running, stopping, stopped), cancels spot requests
            - sets our Lambdas (tagged resource-group) to 0 concurrency: nothing of ours runs
            - deletes the runner AMIs (tag resource-group=build-runner) and their snapshots
            - deletes unattached disks tagged resource-group
            - releases Elastic IPs that aren't attached to anything
            - deletes interface VPC endpoints (gateway endpoints are free, and stay)
  fast    a CloudWatch alarm (SNS topic guardrail-fast-stop): something is spending fast, and billing data is
          a day behind. Terminates instances, cancels spot requests, throttles our Lambdas. Keeps AMIs, disks,
          IPs and endpoints, so a false alarm costs only the interrupted jobs. An alarm going back to OK does nothing.
Invoke with {"dry_run": true} (optionally "mode": "fast") to see what it would do without doing it.
Lambdas are only in the home region (the organization blocks Lambda elsewhere); EC2 is swept in every REGION.
"""
import json
import os

import boto3

REGIONS = os.environ.get("REGIONS", "ap-southeast-2").split(",")
HOME = REGIONS[0]
FAST_TOPIC = "guardrail-fast-stop"
GROUP_TAG = "resource-group"


def flag(name):
    return os.environ.get(name) == "true"


def pages(client, method, key, **kwargs):
    for page in client.get_paginator(method).paginate(**kwargs):
        yield from page[key]


def mode_of(event):
    """'budget', 'fast', or None (nothing to do: an alarm going back to OK)."""
    if not isinstance(event, dict):
        return "budget"
    if event.get("mode") in ("budget", "fast"):
        return event["mode"]
    for record in event.get("Records", []):
        sns = record.get("Sns") or {}
        if sns.get("TopicArn", "").endswith(f":{FAST_TOPIC}"):
            try:
                state = json.loads(sns.get("Message") or "{}").get("NewStateValue")
            except ValueError:
                state = "ALARM"  # can't tell: act, it's the safe side
            return "fast" if state == "ALARM" else None
    return "budget"


def stop_instances(ec2, dry_run, done):
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


def delete_amis(ec2, dry_run, done):
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


def delete_detached_volumes(ec2, dry_run, done):
    for v in pages(ec2, "describe_volumes", "Volumes",
                   Filters=[{"Name": "status", "Values": ["available"]}, {"Name": "tag-key", "Values": [GROUP_TAG]}]):
        if not dry_run:
            ec2.delete_volume(VolumeId=v["VolumeId"])
        done["volumes"].append(v["VolumeId"])


def release_unused_addresses(ec2, dry_run, done):
    for a in ec2.describe_addresses()["Addresses"]:
        if a.get("AssociationId") or not a.get("AllocationId"):
            continue
        if not dry_run:
            ec2.release_address(AllocationId=a["AllocationId"])
        done["addresses"].append(a["AllocationId"])


def delete_interface_endpoints(ec2, dry_run, done):
    ids = [e["VpcEndpointId"] for e in pages(ec2, "describe_vpc_endpoints", "VpcEndpoints",
                                             Filters=[{"Name": "vpc-endpoint-type", "Values": ["Interface"]}])
           if e.get("State", "").lower() not in ("deleting", "deleted")]
    if ids and not dry_run:
        for start in range(0, len(ids), 25):
            ec2.delete_vpc_endpoints(VpcEndpointIds=ids[start:start + 25])
    done["endpoints"] = ids


def throttle_lambdas(region, dry_run, done):
    tagging = boto3.client("resourcegroupstaggingapi", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    for r in pages(tagging, "get_resources", "ResourceTagMappingList",
                   ResourceTypeFilters=["lambda:function"], TagFilters=[{"Key": GROUP_TAG}]):
        name = r["ResourceARN"].rsplit(":", 1)[-1]
        if name.startswith("guardrail-"):
            continue  # never ourselves (the guardrails aren't tagged anyway)
        if not dry_run:
            lam.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=0)
        done["lambdas_throttled"].append(name)


def stop_region(region, mode, dry_run):
    ec2 = boto3.client("ec2", region_name=region)
    done = {k: [] for k in ("instances", "spot_requests", "lambdas_throttled", "amis", "snapshots",
                            "volumes", "addresses", "endpoints", "errors")}
    steps = []
    if flag("TERMINATE_INSTANCES"):
        steps.append(lambda: stop_instances(ec2, dry_run, done))
    if flag("THROTTLE_LAMBDAS") and region == HOME:
        steps.append(lambda: throttle_lambdas(region, dry_run, done))
    if mode == "budget":
        if flag("DELETE_RUNNER_AMIS"):
            steps.append(lambda: delete_amis(ec2, dry_run, done))
        if flag("DELETE_DETACHED_VOLUMES"):
            steps.append(lambda: delete_detached_volumes(ec2, dry_run, done))
        if flag("RELEASE_UNUSED_ADDRESSES"):
            steps.append(lambda: release_unused_addresses(ec2, dry_run, done))
        if flag("DELETE_INTERFACE_ENDPOINTS"):
            steps.append(lambda: delete_interface_endpoints(ec2, dry_run, done))
    for step in steps:  # one failing step mustn't stop the others; the log says what failed
        try:
            step()
        except Exception as e:
            done["errors"].append(f"{type(e).__name__}: {e}")
    return done


def handler(event, context):
    dry_run = isinstance(event, dict) and bool(event.get("dry_run"))
    mode = mode_of(event)
    if mode is None:
        result = {"mode": None, "note": "alarm back to OK: nothing to do"}
    else:
        result = {"mode": mode, "dry_run": dry_run, "regions": {r: stop_region(r, mode, dry_run) for r in REGIONS}}
    print(json.dumps(result))
    return result
