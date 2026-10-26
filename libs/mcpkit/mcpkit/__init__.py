"""mcpkit: a deliberately small MCP server toolkit (Streamable HTTP, tools only).

Why hand-rolled instead of an SDK: the assignment asks us to understand the
protocol flow end to end, and the surface we need (initialize, ping,
tools/list, tools/call) is small enough to implement and test in ~250 lines.
"""
from .server import McpApp, ToolError, SUPPORTED_PROTOCOL_VERSIONS  # noqa: F401
