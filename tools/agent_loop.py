"""Gemini-driven CAD agent loop — autonomous CAD generation from natural language briefs.

`cad_driver.py` is a scripted driver executing explicit tool steps. THIS module
is an autonomous Gemini agent driving the MCP tools end-to-end from only a
natural-language brief.

Usage:
    export GEMINI_API_KEY=...
    uv run python tools/agent_loop.py \\
        --brief "Build a 40x40x10mm plate with a central 8mm through-hole."

The loop:
    1. Creates a throwaway Onshape document + Part Studio.
    2. Seeds Gemini with the SKILL.md protocols + document/workspace/element
       IDs + the brief.
    3. Gemini picks tool calls. We execute them via server.call_tool().
       Tool results — including rendered PNGs from describe_part_studio —
       go back into the conversation as function responses + image blobs.
    4. Loops until Gemini stops calling tools (i.e. reaches plain text DONE)
       or we hit MAX_TURNS.
    5. Every tool call + response is logged; every rendered image is
       saved to the session output directory so the run is post-mortemable.

The agent sees the exact same tools any future MCP client of this
server would see, via the server's own list_tools() output.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO.parent.parent / ".env")
load_dotenv(REPO / ".env")

from loguru import logger  # noqa: E402

from onshape_mcp import server as S  # noqa: E402
from onshape_mcp.api.client import OnshapeClient, OnshapeCredentials  # noqa: E402
from onshape_mcp.api.documents import DocumentManager  # noqa: E402
from onshape_mcp.api.partstudio import PartStudioManager  # noqa: E402

SKILL_PATH = REPO.parent.parent / "SKILL.md"

# Tools we want the agent to see. Deliberately a subset: focus on Part
# Studio authoring + describe/measure + utilities; skip the assembly tools
# (the agent-loop demos are single-part). Expand this list when demo scope
# grows.
AGENT_TOOLS = [
    "create_sketch_rectangle",
    "create_sketch_circle",
    "create_sketch_line",
    "create_sketch_arc",
    "create_extrude",
    "create_revolve",
    "create_thicken",
    "create_fillet",
    "create_chamfer",
    "create_boolean",
    "create_linear_pattern",
    "create_circular_pattern",
    "update_feature",
    "delete_feature_by_name",
    "list_entities",
    "describe_part_studio",
    "measure",
    "get_mass_properties",
    "render_part_studio_views",
    "crop_image",
    "list_cached_images",
    "export_part_studio",
]


def _load_skill_md() -> str:
    try:
        return SKILL_PATH.read_text()
    except OSError:
        return "(SKILL.md not found — running without skill guide)"


def _sanitize_schema_for_genai(s: Any) -> Any:
    """Recursively convert JSON Schema into Google GenAI compatible format.
    Strips unsupported attributes and maps union types to single valid types.
    """
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k == "type":
            if isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                v = non_null[0] if non_null else "string"
            out["type"] = v.upper()
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: _sanitize_schema_for_genai(pv) for pk, pv in v.items()}
        elif k == "items":
            out["items"] = _sanitize_schema_for_genai(v)
        elif k in ("description", "required", "enum"):
            out[k] = v
    return out


async def _mcp_tools_as_genai_declarations() -> List[Any]:
    from google.genai import types

    mcp_tools = await S.list_tools()
    filtered = [t for t in mcp_tools if t.name in AGENT_TOOLS]
    declarations = []
    for t in filtered:
        raw_schema = dict(t.inputSchema) if hasattr(t, "inputSchema") else {}
        cleaned_schema = _sanitize_schema_for_genai(raw_schema)
        declarations.append(
            types.FunctionDeclaration(
                name=t.name,
                description=t.description or "",
                parameters=cleaned_schema,
            )
        )
    return declarations


async def run_agent(
    brief: str,
    out_dir: Path,
    *,
    model: str = "gemini-3.6-flash",
    max_turns: int = 30,
) -> Dict[str, Any]:
    """Run the Gemini CAD agent loop. Returns a summary dict."""

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. The agent-loop needs Google GenAI API access. "
            "Export the key and retry."
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError(
            "google-genai SDK not installed. Run: uv add google-genai"
        )

    ak = os.getenv("ONSHAPE_API_KEY") or os.getenv("ONSHAPE_ACCESS_KEY", "")
    sk = os.getenv("ONSHAPE_API_SECRET") or os.getenv("ONSHAPE_SECRET_KEY", "")
    creds = OnshapeCredentials(access_key=ak, secret_key=sk)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "brief.txt").write_text(brief)
    transcript_path = out_dir / "transcript.jsonl"
    transcript_path.write_text("")

    def _log_event(kind: str, data: Dict[str, Any]) -> None:
        with transcript_path.open("a") as f:
            f.write(json.dumps({"kind": kind, **data}, default=str) + "\n")

    async with OnshapeClient(creds) as client:
        dm = DocumentManager(client)
        doc_name = f"dyna-agent {int(time.time())}"
        doc = await dm.create_document(doc_name)
        did = doc.id
        workspaces = await dm.get_workspaces(did)
        wid = next((w.id for w in workspaces if w.is_main), workspaces[0].id)
        psm = PartStudioManager(client)
        ps = await psm.create_part_studio(did, wid, "agent ps")
        eid = ps["id"] if isinstance(ps, dict) else ps.id
        doc_url = f"https://cad.onshape.com/documents/{did}/w/{wid}/e/{eid}"
        logger.info(f"agent doc: {doc_url}")
        _log_event("doc_created", {"did": did, "wid": wid, "eid": eid, "url": doc_url})

        fn_declarations = await _mcp_tools_as_genai_declarations()
        skill = _load_skill_md()

        system_prompt = (
            "You are an engineering CAD designer driving an Onshape Part Studio "
            "through a tool interface. Your job is to build the part described by "
            "the user's brief.\n\n"
            "Design context:\n"
            f"  documentId = {did}\n"
            f"  workspaceId = {wid}\n"
            f"  elementId = {eid}\n"
            f"  (pass these into every tool call's documentId/workspaceId/elementId fields)\n\n"
            "Protocol guide (MUST follow):\n" + skill + "\n\n"
            "Work iteratively: (1) decide the next feature, (2) call the tool, "
            "(3) describe_part_studio after any non-trivial change, (4) verify "
            "in the returned text + render, (5) fix or proceed. When the part "
            "is complete and verified, call export_part_studio once to produce "
            "a STEP file, then state DONE in plain text and stop using tools."
        )

        ai_client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=fn_declarations)],
            temperature=0.2,
        )
        chat = ai_client.chats.create(model=model, config=config)

        turn = 0
        current_input = f"Brief: {brief}\n\nBegin. Use tools. Describe after every mutation."

        while turn < max_turns:
            turn += 1
            logger.info(f"=== TURN {turn} ===")
            _log_event("turn_start", {"turn": turn})

            resp = chat.send_message(current_input)

            # Log text content
            if resp.text:
                _log_event("assistant_text", {"turn": turn, "text": resp.text})
                logger.info(f"assistant: {resp.text[:400]}")

            # Check for function calls
            function_calls = resp.function_calls
            if not function_calls:
                logger.info("No further function calls — ending loop.")
                _log_event("stop", {"reason": "completed", "final_text": resp.text})
                break

            response_parts = []
            img_ix = 0

            for call in function_calls:
                call_args = dict(call.args) if call.args else {}
                _log_event("tool_use", {
                    "turn": turn,
                    "name": call.name,
                    "input": call_args,
                })
                logger.info(f"tool_use: {call.name}({json.dumps(call_args, default=str)[:200]})")

                try:
                    result_blocks = await S.call_tool(call.name, call_args)
                except Exception as e:
                    result_blocks = [
                        type("_", (), {
                            "type": "text",
                            "text": f"tool raised: {type(e).__name__}: {e}",
                        })()
                    ]

                result_text = ""
                for block in result_blocks:
                    btype = getattr(block, "type", None)
                    if btype == "text":
                        result_text += block.text + "\n"
                    elif btype == "image":
                        try:
                            png_bytes = base64.b64decode(block.data)
                            out_path = out_dir / f"{turn:02d}_tool{img_ix}.png"
                            out_path.write_bytes(png_bytes)
                            img_ix += 1
                            response_parts.append(
                                types.Part.from_bytes(data=png_bytes, mime_type=block.mimeType)
                            )
                        except Exception as e:
                            logger.warning(f"failed to save tool image: {e}")

                response_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"output": result_text.strip()},
                    )
                )

                _log_event("tool_result", {
                    "turn": turn,
                    "name": call.name,
                    "text_preview": result_text[:300],
                })

            current_input = response_parts

        summary = {
            "doc_url": doc_url,
            "document_id": did,
            "turns": turn,
            "model": model,
            "out_dir": str(out_dir),
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        return summary


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Gemini-driven CAD agent loop")
    parser.add_argument("--brief", required=True, help="Natural-language CAD brief")
    parser.add_argument("--model", default="gemini-3.6-flash", help="Gemini model identifier (e.g. gemini-3.6-flash, gemini-3.8-flash)")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument(
        "--out",
        default=str(REPO.parent.parent / "scratchpad" / "agent-runs"),
        help="Directory root for per-run output",
    )
    args = parser.parse_args()
    ts = int(time.time())
    out_dir = Path(args.out) / f"run-{ts}"

    summary = asyncio.run(run_agent(
        brief=args.brief,
        out_dir=out_dir,
        model=args.model,
        max_turns=args.max_turns,
    ))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
