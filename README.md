# onshape-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude and other AI agents build, inspect and edit parametric CAD in [Onshape](https://www.onshape.com): sketches, extrudes, fillets, patterns, assemblies and mates, variables, and custom FeatureScript.

Every tool that changes the model returns a structured report of what actually changed, which warnings Onshape's regenerator raised, and hints for the next step, so an agent can check its own work instead of assuming a call succeeded.

> Started as a fork of [hedless/onshape-mcp](https://github.com/hedless/onshape-mcp) (MIT). The REST client, HMAC auth and first feature builders come from upstream; see [NOTICE](NOTICE) for what was added in this fork.

## What it can do (69 tools)

| Area | Examples |
|---|---|
| Documents | create, search, summarise, list Part Studios and elements |
| Sketches | lines, arcs, circles, rectangles, rounded rectangles, constraints, edit and inspect existing sketches |
| Features | extrude, revolve, fillet, chamfer, shell, thicken, boolean, linear and circular patterns, offset planes |
| Parametrics | Variable Studios, read and set variables, update or delete features by name |
| FeatureScript | evaluate FeatureScript, write and apply custom features |
| Assemblies | add instances, fastened, revolute, slider and cylindrical mates, positioning, interference checks |
| Analysis | mass properties, bounding boxes, measurements, entity discovery with face coordinate systems |
| Vision | render Part Studios, assemblies and sketches from several views; crop and load images; extract dimensions from drawings; compare a build against a reference |
| Export | Part Studio and assembly export (e.g. STEP, STL) |

## Quick start

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/MuditAtrey/onshape-mcp
cd onshape-mcp
cp .env.example .env   # add your Onshape API keys from https://dev-portal.onshape.com/keys
uv sync
uv run onshape-mcp
```

To use it from an MCP client, add the server to the client's config (see [`mcp_config.json`](mcp_config.json)):

```json
{
  "mcpServers": {
    "onshape": {
      "command": "uv",
      "args": ["run", "onshape-mcp"],
      "env": {
        "ONSHAPE_ACCESS_KEY": "${ONSHAPE_ACCESS_KEY}",
        "ONSHAPE_SECRET_KEY": "${ONSHAPE_SECRET_KEY}"
      }
    }
  }
}
```

The repo also ships a Claude Code plugin manifest in [`.claude-plugin/`](.claude-plugin/).

## Agent harness and benchmark

[`tools/`](tools/) contains agent loops (plain API and Claude Agent SDK) that run design briefs end to end and export STEP files.

[RESEARCH.md](RESEARCH.md) reports a benchmark of an agent using these tools on real engineering parts, graded against reference STEP files on volume, bounding box, topology, Boolean IoU and Chamfer distance. The main finding: the agent **executes CAD reliably but does not yet read engineering drawings reliably**. With a human-written spec it scored 1.000 on the simpler set; with a spec it extracted from the drawing itself, 0.533.

## Tests

```bash
uv run --extra dev pytest
```

698 tests, all passing as of November 2026. They mock the Onshape API, so no account or keys are needed.

## Project layout

```
onshape_mcp/
  server.py      MCP server and tool definitions
  api/           Onshape REST client, documents, Part Studios, assemblies, rendering, FeatureScript
  builders/      feature payload builders (sketch, extrude, fillet, mate, ...)
  analysis/      face coordinate systems, interference, positioning
tools/           agent loops for running design briefs
tests/           unit tests (mocked API)
knowledge_base/  workflow notes for agents
```

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
