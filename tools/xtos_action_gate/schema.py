"""Bounded exact request schema; paths are lexical, not filesystem identities."""
import json
import posixpath
from types import MappingProxyType

MAX_JSON = 65536


class GateError(ValueError):
    """Approval denied; no executor call occurred."""


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise GateError("invalid JSON") from exc


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GateError("duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise GateError("non-finite JSON")
    try:
        if type(raw) not in (str, bytes) or len(raw) > MAX_JSON:
            raise GateError("oversized or invalid JSON")
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise GateError("invalid JSON") from exc


def keys(value, expected):
    if type(value) is not dict or set(value) != set(expected.split()):
        raise GateError("unexpected schema fields")


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise GateError("invalid integer")


def text(value, maximum=4096, empty=False):
    if type(value) is not str or len(value) > maximum or (not empty and not value):
        raise GateError("invalid string")
    if any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise GateError("invalid control or surrogate")


def path(value):
    text(value)
    if (not value.startswith("/") or value.startswith("//") or
            posixpath.normpath(value) != value or ".." in value.split("/")):
        raise GateError("non-canonical absolute path")


def snapshot(request):
    # Serialize/copy first: execution never reads the caller's mutable object again.
    raw = request if type(request) in (str, bytes) else canonical(request)
    req = loads(raw)
    keys(req, "schema argv root cwd uid gid limits timeout_ms writable_dirs sandbox_policy")
    if req["schema"] != "xtos.action.v1":
        raise GateError("unknown request schema")
    argv = req["argv"]
    if type(argv) is not list or not 1 <= len(argv) <= 128:
        raise GateError("invalid argv")
    for arg in argv:
        text(arg, empty=True)
    path(argv[0])
    path(req["root"])
    path(req["cwd"])
    if req["root"] != "/" and not (req["cwd"] == req["root"] or
            req["cwd"].startswith(req["root"] + "/")):
        raise GateError("cwd outside root")
    for field in ("uid", "gid"):
        integer(req[field], 1, 2**32 - 2)
    keys(req["limits"], "cpu_sec as_bytes nofile nproc")
    for value in req["limits"].values():
        integer(value, 1, 2**63 - 2)
    integer(req["timeout_ms"], 1, 86400000)
    dirs = req["writable_dirs"]
    if type(dirs) is not list or len(dirs) > 16:
        raise GateError("invalid writable directories")
    for directory in dirs:
        path(directory)
    if len(set(dirs)) != len(dirs):
        raise GateError("duplicate writable directory")
    policy = req["sandbox_policy"]
    keys(policy, "version clear_groups deny_socket deny_getpid")
    if policy["version"] != "xtos.runner.v1" or policy["clear_groups"] is not True:
        raise GateError("unsupported sandbox")
    for field in ("deny_socket", "deny_getpid"):
        if type(policy[field]) is not bool:
            raise GateError("invalid sandbox boolean")
    return canonical(req), freeze(req)


def freeze(value):
    if type(value) is dict:
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if type(value) is list:
        return tuple(freeze(v) for v in value)
    return value
