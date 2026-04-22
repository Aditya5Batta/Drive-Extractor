"""
MCP tool layer — schemas, handlers, and the shared tool context.

Usage from server.py:
    from tools import TOOL_SCHEMAS, dispatch, build_context
    ctx = build_context()
    ...
    return TOOL_SCHEMAS        # in @server.list_tools()
    return await dispatch(name, arguments, ctx)  # in @server.call_tool()
"""
from tools.schemas import ALL_TOOL_SCHEMAS as TOOL_SCHEMAS
from tools.handlers import dispatch
from tools.context import ToolContext, build_context

__all__ = ["TOOL_SCHEMAS", "dispatch", "ToolContext", "build_context"]
