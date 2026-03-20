from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid


def _emit_local_probe(endpoint: str, probe_id: str) -> tuple[bool, str]:
    os.environ.setdefault("OTEL_ENABLED", "true")
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    os.environ.setdefault("OTEL_SERVICE_NAME", "otel-export-verifier")
    try:
        from services.otel_setup import configure_otel, force_flush_otel, span

        configure_otel("otel-export-verifier")
        with span("otel.explicit_probe", probe_id=probe_id, verifier="verify_otel_export.py"):
            pass
        force_flush_otel()
        return True, "local-sdk"
    except Exception:
        return False, "local-sdk-unavailable"


def _emit_runtime_probe(probe_url: str, api_key: str, probe_id: str) -> tuple[bool, str]:
    req = urllib.request.Request(
        probe_url,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": api_key, "X-Trace-Id": probe_id},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        return body.get("ok") is True, "runtime-http"
    except Exception:
        return False, "runtime-http-unavailable"


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit an explicit OTEL span and verify it in the collector exporter output.")
    parser.add_argument("--collector-container", default="sinc-otel-collector")
    parser.add_argument("--tail", type=int, default=200)
    parser.add_argument("--sleep-seconds", type=float, default=2.5)
    parser.add_argument("--endpoint", default=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317"))
    parser.add_argument("--collector-trace-file", default="/var/lib/otel/traces.jsonl")
    parser.add_argument("--probe-url", default=os.environ.get("OTEL_PROBE_URL", "http://127.0.0.1:8765/otel/probe"))
    parser.add_argument("--api-key", default=os.environ.get("OTEL_PROBE_API_KEY", "dev"))
    args = parser.parse_args()

    probe_id = f"probe-{uuid.uuid4().hex[:12]}"
    ok_emit, emit_source = _emit_local_probe(args.endpoint, probe_id)
    if not ok_emit:
        ok_emit, emit_source = _emit_runtime_probe(args.probe_url, args.api_key, probe_id)
    if not ok_emit:
        print(f"probe_id={probe_id}", flush=True)
        print(f"emit_source={emit_source}", flush=True)
        print("status=emit_failed", flush=True)
        return 2

    time.sleep(max(args.sleep_seconds, 0.5))

    combined = ""
    probe_source = "collector-file"
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                args.collector_container,
                "sh",
                "-lc",
                f"tail -n {int(args.tail)} {args.collector_trace_file} 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        combined = (result.stdout or "") + (result.stderr or "")
    except Exception:
        combined = ""

    if not combined.strip():
        probe_source = "collector-logs-fallback"
        try:
            result = subprocess.run(
                ["docker", "logs", args.collector_container, "--tail", str(args.tail)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            combined = (result.stdout or "") + (result.stderr or "")
        except Exception as exc:
            print(f"probe_id={probe_id}", flush=True)
            print(f"emit_source={emit_source}", flush=True)
            print(f"probe_source={probe_source}", flush=True)
            print("status=unknown", flush=True)
            print(f"reason=collector_artifacts_unavailable:{exc}", flush=True)
            return 2

    ok = "otel.explicit_probe" in combined and probe_id in combined
    print(f"probe_id={probe_id}", flush=True)
    print(f"collector_container={args.collector_container}", flush=True)
    print(f"emit_source={emit_source}", flush=True)
    print(f"probe_source={probe_source}", flush=True)
    print(f"endpoint={args.endpoint}", flush=True)
    print(f"status={'ok' if ok else 'missing'}", flush=True)
    if not ok:
        excerpt = combined[-4000:] if combined else "<no collector logs>"
        print(excerpt, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
