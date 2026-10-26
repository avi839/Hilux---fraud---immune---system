"""Req 27.8: with the Narrative_Generator disabled, HELIX sends no data to any LLM endpoint.

Honest scope: the Narrative_Generator (Req 26, optional) is not implemented, so HELIX has
no LLM client at all. This test pins that down statically: no module under ``helix/``
imports an LLM SDK or a general-purpose outbound HTTP/socket client, and no module names a
well-known hosted LLM endpoint. That is sufficient for "disabled => no LLM traffic" only
while no such client exists. When the Narrative_Generator is added, its module must be the
single allowed importer here, and a runtime test must assert zero outbound calls with it
disabled. This is a static check, not a network sandbox.
"""

from __future__ import annotations

import ast
from pathlib import Path

HELIX = Path(__file__).resolve().parents[2] / "helix"

LLM_SDKS = {
    "openai", "anthropic", "litellm", "langchain", "langchain_core", "langchain_community", "langchain_openai",
    "llama_index", "cohere", "mistralai", "groq", "together", "ollama", "huggingface_hub", "vertexai",
    "google.generativeai", "google.genai", "google.cloud.aiplatform", "boto3", "botocore",
}
HTTP_CLIENTS = {"httpx", "requests", "aiohttp", "urllib3", "urllib.request", "http.client", "socket",
                "websockets", "pycurl"}
FORBIDDEN = LLM_SDKS | HTTP_CLIENTS
LLM_HOSTS = ("api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com", "bedrock-runtime",
             "api.cohere.", "api.mistral.ai", "api.groq.com", "openai.azure.com")


def _forbidden(module: str) -> bool:
    return any(module == f or module.startswith(f + ".") for f in FORBIDDEN)


def _imports(tree: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.lineno, node.module))
            out += [(node.lineno, f"{node.module}.{a.name}") for a in node.names]  # from urllib import request
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("__import__", "import_module"):
                out.append((node.lineno, node.args[0].value))
    return out


def _sources() -> list[Path]:
    files = sorted(HELIX.rglob("*.py"))
    assert files, f"no sources found under {HELIX}"
    return files


def test_no_helix_module_imports_an_llm_sdk_or_http_client():
    bad = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))  # some files carry a BOM
        bad += [f"{path.relative_to(HELIX.parent)}:{ln}: {mod}" for ln, mod in _imports(tree) if _forbidden(mod)]
    assert not bad, "outbound client imported (Req 27.8):\n" + "\n".join(bad)


def test_no_helix_module_names_an_llm_endpoint():
    bad = []
    for path in _sources():
        text = path.read_text(encoding="utf-8-sig").lower()
        bad += [f"{path.relative_to(HELIX.parent)}: {h}" for h in LLM_HOSTS if h in text]
    assert not bad, "LLM endpoint referenced (Req 27.8):\n" + "\n".join(bad)


def test_scanner_detects_forbidden_imports():
    src = "import openai\nfrom urllib import request\nimport importlib\nimportlib.import_module('httpx')\n"
    mods = sorted(m for _, m in _imports(ast.parse(src)) if _forbidden(m))
    assert mods == ["httpx", "openai", "urllib.request"]
    assert _forbidden("openai.types") and _forbidden("google.generativeai")
    assert not _forbidden("helix.api.security") and not _forbidden("requests_like") and not _forbidden("urllib.parse")
