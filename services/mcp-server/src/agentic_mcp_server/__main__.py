"""Container entrypoint: serve the Context Broker over streamable HTTP."""

from agentic_mcp_server.mcp.server import create_app


def main() -> None:
    create_app().run(transport="http", host="0.0.0.0", port=8000, path="/mcp/")


if __name__ == "__main__":
    main()
