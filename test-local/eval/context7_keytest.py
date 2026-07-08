#!/usr/bin/env python3
"""Targeted test: does casa's op://Context7/credential key authenticate to context7?

Reads ONLY the Context7/credential item via casa's op service account (no vault
enumeration), then compares three context7 calls — real key / bogus key / keyless —
to see whether the key is honored (bogus rejected? rate-limits raised?). The key
value is never printed.
"""
import json, os, subprocess, urllib.request, urllib.error


try:
    op_token = json.load(open("/data/options.json")).get("onepassword_service_account_token", "")
except Exception:
    op_token = ""
ref = "op://Casa/Context7/credential"
env = {**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": op_token}
try:
    key = subprocess.run(["op", "read", ref], env=env, capture_output=True,
                         text=True, timeout=30, check=True).stdout.strip()
except subprocess.CalledProcessError as e:
    print("op read FAILED for", ref, "->", (e.stderr or "")[:200])
    raise SystemExit(1)
print(f"resolved {ref} -> key length {len(key)} (value not printed)")

BASE = "https://mcp.context7.com/mcp"


def run_call(api_key, label):
    def post(body, sid=None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if api_key:
            h["CONTEXT7_API_KEY"] = api_key
        if sid:
            h["Mcp-Session-Id"] = sid
        req = urllib.request.Request(BASE, data=json.dumps(body).encode(),
                                     method="POST", headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=30)
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in (e.headers.items())}, e.read().decode()

    st, hdr, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "casa", "version": "0"}}})
    sid = hdr.get("mcp-session-id")
    if sid:
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    st2, hdr2, b2 = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "resolve-library-id",
                                     "arguments": {"query": "s3", "libraryName": "boto3"}}}, sid)
    rl = {k: v for k, v in hdr2.items() if "ratelimit" in k or "rate-limit" in k}
    is_err, snip = None, ""
    for line in b2.splitlines():
        if line.startswith("data:"):
            d = json.loads(line[5:])
            res = d.get("result", {})
            is_err = res.get("isError")
            snip = (res.get("content", [{}])[0].get("text", "") or "")[:90]
    print(f"[{label:14}] init={st} call={st2} isError={is_err} rate_limit={rl} data={snip!r}")


run_call(key, "REAL KEY")
run_call("ctx7_invalid_bogus_key_0000000000", "BOGUS KEY")
run_call(None, "KEYLESS")
