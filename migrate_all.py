"""
migrate_all.py - Auto-generated documentation base.
"""

import os
import re
from pathlib import Path
from datetime import datetime, timezone
import json

def migrate():
    nodes_dir = Path(os.path.join(os.environ.get("TALA_WORKSPACE_ROOT", "."), "nodes"))
    for file_path in nodes_dir.glob("*.html"):
        content = file_path.read_text(encoding="utf-8")
        
        # Check if already migrated
        if '<meta name="tala-schema" content="neural-node/v1">' in content:
            continue
            
        # Extract title
        title_match = re.search(r'<title>(.*?)</title>', content)
        title = title_match.group(1).strip() if title_match else file_path.stem
        
        # Extract node_id
        id_match = re.search(r'data-node-id=[\'"]([^\'"]+)[\'"]', content)
        node_id = id_match.group(1) if id_match else file_path.stem
        
        # Extract node_type
        type_match = re.search(r'data-node-type=[\'"]([^\'"]+)[\'"]', content)
        node_type = type_match.group(1) if type_match else "concept"
        
        # Extract execution_authority
        auth_match = re.search(r'data-execution-authority=[\'"]([^\'"]+)[\'"]', content)
        authority = auth_match.group(1) if auth_match else "0.50"
        
        # Extract inner content
        # We try to get everything inside <article>
        article_match = re.search(r'<article[^>]*>(.*?)</article>', content, re.DOTALL)
        inner_html = article_match.group(1).strip() if article_match else content
        
        # remove h1 if it's there
        inner_html = re.sub(r'<h1[^>]*>.*?</h1>', '', inner_html, count=1, flags=re.DOTALL | re.IGNORECASE).strip()
        
        current_iso_time = datetime.now(timezone.utc).isoformat()
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        new_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="tala-schema" content="neural-node/v1">
  <meta name="tala-node-id" content="{node_id}">
  <meta name="tala-node-type" content="{node_type}">
  <meta name="tala-authority" content="{authority}">
  <meta name="tala-created" content="{current_iso_time}">
  <meta name="tala-author" content="tala-agent">
  <title>{title}</title>
</head>
<body>
<article data-node-id="{node_id}"
         data-node-type="{node_type}"
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
{inner_html}
  </section>
 
  <script type="application/json" id="node-state">
  {{
    "title": {json.dumps(title)},
    "node_type": {json.dumps(node_type)},
    "raw_markdown": {json.dumps(inner_html)}
  }}
  </script>
</article>
</body>
</html>
"""
        file_path.write_text(new_html, encoding="utf-8")
        print(f"Migrated {file_path.name}")

if __name__ == "__main__":
    migrate()
