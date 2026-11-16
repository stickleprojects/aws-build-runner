#!/usr/bin/env python3
"""Talk to SSM Parameter Store from the bake instance, with nothing installed yet.

  report.py STATUS [DETAIL_FILE]   put /build/bake/result/<instance-id>: STATUS (STARTED, OK or FAILED)
                                   plus the last ~3 KB of DETAIL_FILE (a log, or the manifest)
  report.py --get NAME             print a parameter's value, decrypted (e.g. /build/bake/github-token)

Standard library only: credentials from the instance role via IMDSv2, and the SSM request signed with
SigV4 by hand. bake.sh reads the result (EC2's console capture turned out to be empty for these instances).
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


def instance_context():
    token = urllib.request.urlopen(urllib.request.Request(
        IMDS + "api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}),
        timeout=5).read().decode()
    role = imds("meta-data/iam/security-credentials/", token).splitlines()[0]
    return {"instance": imds("meta-data/instance-id", token),
            "region": imds("meta-data/placement/region", token),
            "creds": json.loads(imds(f"meta-data/iam/security-credentials/{role}", token))}


def sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def ssm_call(region, creds, target, payload):
    host = f"ssm.{region}.amazonaws.com"
    body = json.dumps(payload)
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"content-type": "application/x-amz-json-1.1", "host": host, "x-amz-date": amz_date,
               "x-amz-target": f"AmazonSSM.{target}"}
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
    return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")


def put_parameter(region, creds, name, value):
    ssm_call(region, creds, "PutParameter", {"Name": name, "Value": value, "Type": "String", "Overwrite": True})


def get_parameter(region, creds, name):
    return ssm_call(region, creds, "GetParameter", {"Name": name, "WithDecryption": True})["Parameter"]["Value"]


def main():
    if sys.argv[1] == "--get":
        ctx = instance_context()
        sys.stdout.write(get_parameter(ctx["region"], ctx["creds"], sys.argv[2]))
        return
    status = sys.argv[1]
    detail = ""
    if len(sys.argv) > 2:
        try:
            detail = open(sys.argv[2], errors="replace").read()[-3000:]
        except OSError as e:
            detail = f"(couldn't read {sys.argv[2]}: {e})"
    ctx = instance_context()
    value = json.dumps({"status": status, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                        "detail": detail})[:4096]
    put_parameter(ctx["region"], ctx["creds"], f"/build/bake/result/{ctx['instance']}", value)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # reporting must never break the bake; --get failures are reported by the caller
        print(f"report.py: {e}", file=sys.stderr)
        sys.exit(1 if len(sys.argv) > 1 and sys.argv[1] == "--get" else 0)
