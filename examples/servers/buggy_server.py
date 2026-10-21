"""An example MCP server with the bugs amagi-mcp-probe is built to catch.

    amagi-mcp-probe --timeout 3 -- python examples/servers/buggy_server.py

Each tool is broken in a different, realistic way. Nothing here is random.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from minimcp import InvalidParams, serve, text  # noqa: E402


def echo(args):
    return text(args["message"])


def resize_image(args):
    # Off-by-one: the schema allows width 1, and width 1 divides by zero here.
    scale = 100 / (args["width"] - 1)
    return text(f"scaled by {scale:.2f}")


def fetch_page(args):
    print(f"fetching {args['url']}")  # a debug print on stdout corrupts the JSON-RPC stream
    return text("<html>...</html>")


def weather(args):
    # outputSchema promises temp_c, but the handler now sends `temperature`.
    return text("21 C", {"city": args["city"], "temperature": 21})


def lookup_user(args):
    # The schema says integer; the handler only accepts strings.
    if not isinstance(args["user_id"], str):
        raise InvalidParams("user_id must be a string")
    return text("found")


def slow_report(args):
    time.sleep(3600)  # stands in for a lock that is never released
    return text("done")


def drop_table(args):
    return text(f"dropped {args['table']}")


TOOLS = [
    {
        "name": "echo",
        "description": "Echo a message back.",
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
        "handler": echo,
    },
    {
        "name": "resize_image",
        "description": "Resize an image to the given size.",
        "inputSchema": {
            "type": "object",
            "properties": {"width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}},
            "required": ["width", "height"],
        },
        "handler": resize_image,
    },
    {
        "name": "fetch_page",
        "description": "Fetch a web page.",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "format": "uri"}}, "required": ["url"]},
        "handler": fetch_page,
    },
    {
        "name": "weather",
        "description": "Current weather for a city.",
        "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        "outputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "temp_c": {"type": "number"}},
            "required": ["city", "temp_c"],
        },
        "handler": weather,
    },
    {
        "name": "lookup_user",
        "description": "Look up a user by id.",
        "inputSchema": {"type": "object", "properties": {"user_id": {"type": "integer"}}, "required": ["user_id"]},
        "handler": lookup_user,
    },
    {
        "name": "slow_report",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": slow_report,
    },
    {
        "name": "count_rows",
        "description": "Count rows in a table.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "int"}}},
        "handler": echo,
    },
    {
        "name": "drop_table",
        "description": "Drop a database table.",
        "inputSchema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]},
        "annotations": {"destructiveHint": True},
        "handler": drop_table,
    },
]

if __name__ == "__main__":
    serve("buggy", "0.3.0", TOOLS)
