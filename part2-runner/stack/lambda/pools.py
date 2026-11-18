"""A consumer's runner pools: its pools.json, checked against the platform's cost boundary (#54, phase 1).

The format is agreed with spellbound3d (part2-runner/RUNNER-IMAGE.md here; tools/runner-image/README.md
there). The consumer owns its pools; the platform owns the boundary, the label prefix and the classes.
Anything outside them refuses the WHOLE file (Invalid, with every reason), and the launcher keeps the
last good copy. Pure: no AWS, no GitHub, so it's tested offline and used by CI's cost-boundary rule.

    boundary = {"instance_types": ["t3.*", ...], "instance_sizes": ["micro", ..., "xlarge"],
                "max_runners": 4, "max_instance_minutes": 90, "max_idle_minutes": 20}
"""
import fnmatch
import re

LABEL = re.compile(r"aws-[a-z][a-z0-9-]*")          # platform-owned (labelling standard)
CLASSES = ("light", "heavy")                         # platform-owned: the usage report groups by these
INSTANCE = re.compile(r"([a-z][a-z0-9-]*)\.([0-9]*[a-z]+)")
GRAVITON = re.compile(r"[a-z]+[0-9]+[a-z]*g[a-z]*")  # t4g, c7g, c7gn, m6gd...: arm64; the image is amd64
LIMITS = {"max_runners": "max_runners", "max_instance_minutes": "max_instance_minutes",
          "idle_minutes": "max_idle_minutes"}


class Invalid(Exception):
    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _keys(where, obj, allowed, required, reasons):
    if not isinstance(obj, dict):
        reasons.append(f"{where} isn't an object")
        return False
    for k in obj.keys() - set(allowed):
        reasons.append(f"{where}: unknown key {k!r}")
    for k in set(required) - obj.keys():
        reasons.append(f"{where}: missing {k!r}")
    return True


def _instance_type(where, t, boundary, reasons):
    m = INSTANCE.fullmatch(t) if isinstance(t, str) else None
    if not m:
        reasons.append(f"{where}: {t!r} isn't an instance type")
        return
    family, size = m.groups()
    if GRAVITON.fullmatch(family):
        reasons.append(f"{where}: {t} is Graviton (arm64); the runner image is x86_64 only")
    if not any(fnmatch.fnmatchcase(t, p) for p in boundary["instance_types"]):
        reasons.append(f"{where}: {t} isn't an approved type ({', '.join(boundary['instance_types'])})")
    if size not in boundary["instance_sizes"]:
        reasons.append(f"{where}: {t} is bigger than the boundary allows (up to {boundary['instance_sizes'][-1]})")


def validate(doc, boundary: dict) -> dict:
    """{"pools": {label: {"class", "spot", "on_demand"}}, "limits": {...}} or Invalid with every reason."""
    reasons = []
    if not _keys("pools.json", doc, ("version", "pools", "limits"), ("version", "pools"), reasons):
        raise Invalid(reasons)
    if doc.get("version") != 1 or isinstance(doc.get("version"), bool):
        reasons.append(f"version {doc.get('version')!r} isn't 1")
    pools = doc.get("pools")
    if not isinstance(pools, dict) or not pools:
        reasons.append("pools must be a non-empty object")
        pools = {}
    for label, p in pools.items():
        where = f"pools.{label}"
        if not LABEL.fullmatch(str(label)):
            reasons.append(f"{where}: labels must match {LABEL.pattern}")
        if not _keys(where, p, ("class", "spot", "on_demand"), ("class", "spot", "on_demand"), reasons):
            continue
        if p.get("class") not in CLASSES:
            reasons.append(f"{where}: class {p.get('class')!r} isn't one of {', '.join(CLASSES)}")
        for key, minimum in (("spot", 1), ("on_demand", 0)):
            types = p.get(key)
            if not isinstance(types, list) or len(types) < minimum:
                reasons.append(f"{where}.{key} must be a list of at least {minimum} type(s)")
                continue
            if len(set(map(str, types))) != len(types):
                reasons.append(f"{where}.{key} lists a type twice")
            for t in types:
                _instance_type(f"{where}.{key}", t, boundary, reasons)
    limits = doc.get("limits", {})
    if _keys("limits", limits, LIMITS, (), reasons):
        for key, cap_key in LIMITS.items():
            if key not in limits:
                continue
            v = limits[key]
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                reasons.append(f"limits.{key} must be a positive integer")
            elif v > boundary[cap_key]:
                reasons.append(f"limits.{key} {v} is over the boundary's {boundary[cap_key]}")
    if reasons:
        raise Invalid(reasons)
    return {"pools": {str(k).lower(): {"class": v["class"], "spot": list(v["spot"]), "on_demand": list(v["on_demand"])}
                      for k, v in pools.items()},
            "limits": dict(limits)}


def effective(checked: dict, boundary: dict) -> dict:
    """The limits in force: the consumer's where given (never looser, validate made sure), else the boundary's."""
    lim = checked.get("limits", {})
    return {"max_runners": lim.get("max_runners", boundary["max_runners"]),
            "max_instance_minutes": lim.get("max_instance_minutes", boundary["max_instance_minutes"]),
            "idle_minutes": lim.get("idle_minutes", boundary["max_idle_minutes"])}
