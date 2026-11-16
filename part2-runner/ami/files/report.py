#!/usr/bin/env python3
"""Report the bake's progress to SSM Parameter Store: /build/bake/result/<instance-id>.

  report.py STATUS [DETAIL_FILE]     STATUS: STARTED, OK or FAILED; DETAIL_FILE: its last ~3 KB go along

Standard library only (it runs before anything is installed): credentials from the instance role
via IMDSv2, and the SSM PutParameter request signed with SigV4 by hand. bake.sh reads the result,
because EC2's console capture turned out to be empty for these instances.
"""
import datetime
import hashlib
import hmac
import json
import sys
import urllib.request

IMDS = "http://169.254.169.254/latest/"


def imds(path, token):
    req = urllib.request.Request(IMDS + path, headers={"X-aws-ec2-metadata-token": token})
    return urllib.request.urlopen(req, timeout=5).read().decode()


def sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def put_parameter(region, creds, name, value):
    host = f"ssm.{region}.amazonaws.com"
    body = json.dumps({"Name": name, "Value": value, "Type": "String", "Overwrite": True})
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"content-type": "application/x-amz-json-1.1", "host": host, "x-amz-date": amz_date,
               "x-amz-target": "AmazonSSM.PutParameter"}
    if creds.get("Token"):  # instance-role credentials always have one
        headers["x-amz-security-token"] = creds["Token"]
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["POST", "/", "", "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed,
                           hashlib.sha256(body.encode()).hexdigest()])
    scope = f"{day}/{region}/ssm/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = sign(sign(sign(sign(("AWS4" + creds["SecretAccessKey"]).encode(), day), region), "ssm"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds['AccessKeyId']}/{scope}, "
                                f"SignedHeaders={signed}, Signature={signature}")
    req = urllib.request.Request(f"https://{host}/", data=body.encode(), headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def main():
    status = sys.argv[1]
    detail = ""
    if len(sys.argv) > 2:
        try:
            detail = open(sys.argv[2], errors="replace").read()[-3000:]
        except OSError as e:
            detail = f"(couldn't read {sys.argv[2]}: {e})"
    token = urllib.request.urlopen(urllib.request.Request(
        IMDS + "api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}), timeout=5).read().decode()
    instance = imds("meta-data/instance-id", token)
    region = imds("meta-data/placement/region", token)
    role = imds("meta-data/iam/security-credentials/", token).splitlines()[0]
    creds = json.loads(imds(f"meta-data/iam/security-credentials/{role}", token))
    value = json.dumps({"status": status, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                        "detail": detail})[:4096]
    put_parameter(region, creds, f"/build/bake/result/{instance}", value)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the bake because reporting failed
        print(f"report.py: couldn't report: {e}", file=sys.stderr)
