#!/usr/bin/env python3
"""Fetch the bake bundle from S3 on the bake instance, with nothing installed yet (#48).

    fetch.py BUCKET KEY SHA256 OUT

Travels in the bake's EC2 user data (make-user-data.py), so it stays small: credentials from the
instance role (build-runner-bake, s3:GetObject on bake/* only) via IMDSv2, one SigV4-signed GET,
the SHA-256 checked before anything is unpacked. On failure it reports FAILED to SSM
(/build/bake/result/<instance-id>, as report.py would), so bake.sh stops at once instead of timing out.
"""
import datetime
import hashlib
import hmac
import json
import sys
import time
import urllib.request

IMDS = "http://169.254.169.254/latest/"


def imds(path, token):
    return urllib.request.urlopen(urllib.request.Request(
        IMDS + path, headers={"X-aws-ec2-metadata-token": token}), timeout=5).read().decode()


def context():
    token = urllib.request.urlopen(urllib.request.Request(
        IMDS + "api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}),
        timeout=5).read().decode()
    role = imds("meta-data/iam/security-credentials/", token).splitlines()[0]
    return (imds("meta-data/instance-id", token), imds("meta-data/placement/region", token),
            json.loads(imds(f"meta-data/iam/security-credentials/{role}", token)))


def call(method, host, path, service, region, creds, body=b"", extra=None):
    """One SigV4-signed request; returns the response body."""
    now = datetime.datetime.now(datetime.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    h = {"host": host, "x-amz-date": amz, "x-amz-content-sha256": hashlib.sha256(body).hexdigest(), **(extra or {})}
    if creds.get("Token"):  # instance-role credentials always have one
        h["x-amz-security-token"] = creds["Token"]
    names = ";".join(sorted(h))
    canonical = "\n".join([method, path, "", "".join(f"{k}:{h[k]}\n" for k in sorted(h)), names,
                           h["x-amz-content-sha256"]])
    scope = f"{day}/{region}/{service}/aws4_request"
    key = ("AWS4" + creds["SecretAccessKey"]).encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, "\n".join(["AWS4-HMAC-SHA256", amz, scope,
                                   hashlib.sha256(canonical.encode()).hexdigest()]).encode(), hashlib.sha256).hexdigest()
    h["authorization"] = f"AWS4-HMAC-SHA256 Credential={creds['AccessKeyId']}/{scope}, SignedHeaders={names}, Signature={sig}"
    req = urllib.request.Request(f"https://{host}{path}", data=body or None, headers=h, method=method)
    return urllib.request.urlopen(req, timeout=30).read()


def main(bucket, key, want, out):
    instance, region, creds = context()
    error = "no attempt"
    for attempt in range(5):
        try:
            data = call("GET", f"{bucket}.s3.{region}.amazonaws.com", "/" + key, "s3", region, creds)
            got = hashlib.sha256(data).hexdigest()
            if got != want:
                raise ValueError(f"SHA-256 {got} isn't the expected {want}")
            with open(out, "wb") as f:
                f.write(data)
            return 0
        except Exception as e:  # noqa: BLE001 - report whatever went wrong
            error = f"{type(e).__name__}: {e}"
            time.sleep(5 * (attempt + 1))
    detail = f"fetch.py couldn't get s3://{bucket}/{key}: {error}"
    print(detail, file=sys.stderr)
    body = json.dumps({"Name": f"/build/bake/result/{instance}", "Type": "String", "Overwrite": True,
                       "Value": json.dumps({"status": "FAILED", "detail": detail})}).encode()
    try:
        call("POST", f"ssm.{region}.amazonaws.com", "/", "ssm", region, creds, body,
             {"content-type": "application/x-amz-json-1.1", "x-amz-target": "AmazonSSM.PutParameter"})
    except Exception as e:  # noqa: BLE001
        print(f"and couldn't report it: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))
