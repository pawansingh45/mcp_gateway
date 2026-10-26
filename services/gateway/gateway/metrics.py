"""Tiny Prometheus-text metrics (no client library: three metric families don't justify a dependency)."""
from __future__ import annotations

import threading
from collections import defaultdict

BUCKETS_MS = (50, 100, 250, 500, 1000, 2500, 5000, 10000, 20000)


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.calls: dict[tuple[str, str], int] = defaultdict(int)             # (server, outcome)
        self.buckets: dict[str, list[int]] = defaultdict(lambda: [0] * (len(BUCKETS_MS) + 1))
        self.sum_ms: dict[str, float] = defaultdict(float)
        self.count: dict[str, int] = defaultdict(int)

    def observe_call(self, server: str, outcome: str, duration_ms: float) -> None:
        with self._lock:
            self.calls[(server, outcome)] += 1
            self.count[server] += 1
            self.sum_ms[server] += duration_ms
            idx = next((i for i, b in enumerate(BUCKETS_MS) if duration_ms <= b), len(BUCKETS_MS))
            self.buckets[server][idx] += 1

    def render(self, upstream_status: dict[str, str]) -> str:
        lines = ["# HELP mcp_gateway_tool_calls_total Tool calls by upstream server and outcome.",
                 "# TYPE mcp_gateway_tool_calls_total counter"]
        with self._lock:
            for (server, outcome), n in sorted(self.calls.items()):
                lines.append(f'mcp_gateway_tool_calls_total{{server="{server}",outcome="{outcome}"}} {n}')
            lines += ["# HELP mcp_gateway_tool_call_duration_ms Tool call latency.",
                      "# TYPE mcp_gateway_tool_call_duration_ms histogram"]
            for server, counts in sorted(self.buckets.items()):
                running = 0
                for bound, n in zip(BUCKETS_MS, counts):
                    running += n
                    lines.append(f'mcp_gateway_tool_call_duration_ms_bucket{{server="{server}",le="{bound}"}} {running}')
                lines.append(f'mcp_gateway_tool_call_duration_ms_bucket{{server="{server}",le="+Inf"}} {self.count[server]}')
                lines.append(f'mcp_gateway_tool_call_duration_ms_sum{{server="{server}"}} {self.sum_ms[server]:.1f}')
                lines.append(f'mcp_gateway_tool_call_duration_ms_count{{server="{server}"}} {self.count[server]}')
        lines += ["# HELP mcp_gateway_upstream_up 1 if the upstream is connected.", "# TYPE mcp_gateway_upstream_up gauge"]
        for server, status in sorted(upstream_status.items()):
            lines.append(f'mcp_gateway_upstream_up{{server="{server}",status="{status}"}} {1 if status == "connected" else 0}')
        return "\n".join(lines) + "\n"
