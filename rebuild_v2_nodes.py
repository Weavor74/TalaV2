"""
rebuild_v2_nodes.py - Auto-generated documentation base.
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime, timezone

def rebuild_nodes():
    archive_dir = Path("D:/src/TalaV2/nodes/_archive_legacy")
    out_dir = Path("D:/src/TalaV2/nodes")
    out_dir.mkdir(exist_ok=True)
    
    for file_path in archive_dir.glob("*.html"):
        content = file_path.read_text(encoding="utf-8")
        
        # Extract title
        title_match = re.search(r'<title>(.*?)</title>', content)
        title = title_match.group(1).strip() if title_match else file_path.stem
        
        # Extract node_id
        id_match = re.search(r'data-node-id=[\'"]([^\'"]+)[\'"]', content)
        node_id = id_match.group(1) if id_match else file_path.stem
        
        # Extract node_type
        type_match = re.search(r'data-node-type=[\'"]([^\'"]+)[\'"]', content)
        node_type = type_match.group(1) if type_match else "concept"
        
        # Extract execution_authority (or fallback to legacy meta tag, or default 0.50)
        auth_match = re.search(r'data-execution-authority=[\'"]([^\'"]+)[\'"]', content)
        meta_auth_match = re.search(r'<meta\s+name=[\'"]tala-authority[\'"]\s+content=[\'"]([^\'"]+)[\'"]', content)
        authority = auth_match.group(1) if auth_match else (meta_auth_match.group(1) if meta_auth_match else "0.50")
        
        # Extract inner content
        article_match = re.search(r'<article[^>]*>(.*?)</article>', content, re.DOTALL)
        inner_html = article_match.group(1).strip() if article_match else content
        
        # Extract script blocks to preserve state data and executable code
        state_match = re.search(r'<script type="application/json" id="node-state"[^>]*>(.*?)</script>', inner_html, re.DOTALL)
        state_json = state_match.group(1).strip() if state_match else None
        
        prov_match = re.search(r'<script type="application/json" id="node-provenance"[^>]*>(.*?)</script>', inner_html, re.DOTALL)
        prov_json = prov_match.group(1).strip() if prov_match else None
        
        exec_match = re.search(r'<script type="application/x-python" id="node-executable"[^>]*>(.*?)</script>', inner_html, re.DOTALL)
        exec_code = exec_match.group(1).strip() if exec_match else None
        
        # Clean inner html of script blocks and headers
        clean_html = re.sub(r'<script[^>]*>.*?</script>', '', inner_html, flags=re.DOTALL)
        clean_html = re.sub(r'<header[^>]*>.*?</header>', '', clean_html, flags=re.DOTALL)
        clean_html = re.sub(r'<h1[^>]*>.*?</h1>', '', clean_html, flags=re.DOTALL | re.IGNORECASE).strip()
        
        current_iso_time = datetime.now(timezone.utc).isoformat()
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        # Re-build script blocks
        if not state_json:
            state_data = {
                "title": title,
                "node_type": node_type
            }
            state_json = json.dumps(state_data, indent=2)
            
        script_blocks = f"""  <script type="application/json" id="node-state" data-cognitive-block="state">\n{state_json}\n  </script>"""
        if prov_json:
            script_blocks += f"""\n\n  <script type="application/json" id="node-provenance" data-cognitive-block="provenance">\n{prov_json}\n  </script>"""
        if exec_code:
            script_blocks += f"""\n\n  <script type="application/x-python" id="node-executable">\n{exec_code}\n  </script>"""

        new_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
</head>
<body>
<article data-node-id="{node_id}"
         data-node-type="{node_type}"
         data-schema-version="neural-node/v2"
         data-execution-authority="{authority}"
         data-memory-authority="1.00"
         data-weight-relevance="1.00"
         data-weight-confidence="1.00"
         data-privacy="internal">
 
  <header id="node-identity">
    <h1 id="node-title">{title}</h1>
    <dl>
      <dt>Node ID</dt><dd><code>{node_id}</code></dd>
      <dt>Type</dt><dd><code>{node_type}</code></dd>
      <dt>Created</dt><dd><time datetime="{current_iso_time}">{current_date}</time></dd>
    </dl>
  </header>
 
  <section id="semantic-memory">
{clean_html}
  </section>
 
{script_blocks}
</article>
</body>
</html>
"""
        out_path = out_dir / file_path.name
        out_path.write_text(new_html, encoding="utf-8")
        print(f"Rebuilt {file_path.name} as v2")

if __name__ == "__main__":
    rebuild_nodes()
