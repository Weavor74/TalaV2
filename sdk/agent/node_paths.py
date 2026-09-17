"""Resolve neural-node IDs against the workspace and active portable store."""
from pathlib import Path


def find_neural_node_path(node_id: str, workspace_root: Path) -> Path | None:
    from pathlib import Path
    import re
    from sdk.agent.documentation_nodes import resolve_active_nodes_dir
    clean_id = node_id.strip()
    if clean_id.endswith(".html"):
        clean_id = clean_id[:-5]

    clean_id_path = clean_id.replace("\\", "/")
    stem_id = Path(clean_id_path).name

    # 1. Direct path relative to workspace_root
    direct_candidates = [
        workspace_root / node_id,
        workspace_root / f"{clean_id}.html",
    ]
    for cand in direct_candidates:
        if cand.is_file():
            return cand

    # 2. Candidate directory search
    candidate_dirs = [
        workspace_root / "nodes",
        resolve_active_nodes_dir(),
        workspace_root / "sdk" / "identity",
        workspace_root,
    ]

    for d in candidate_dirs:
        if not d.exists():
            continue
        cands = [
            d / f"{stem_id}.html",
            d / f"{clean_id_path}.html",
            d / f"sdk_agent_{stem_id}.html",
            d / f"{stem_id.replace('-', '_')}.html",
        ]
        for cand in cands:
            if cand.is_file():
                return cand

    # 3. Fallback: Search data-node-id in html files across candidate directories
    node_id_pattern = re.compile(r'data-node-id=["\']([^"\']+)["\']', re.IGNORECASE)
    target_ids = {node_id.lower(), clean_id.lower(), stem_id.lower()}

    for d in candidate_dirs:
        if not d.exists():
            continue
        for html_file in d.glob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="ignore")
                match = node_id_pattern.search(content)
                if match and match.group(1).lower() in target_ids:
                    return html_file
            except Exception:
                pass

    return None


