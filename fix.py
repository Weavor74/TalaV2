"""
fix.py - Auto-generated documentation base.
"""

import sys
with open('renderer/renderer.js', 'r', encoding='utf-8') as f:
    lines = f.readlines()

wiki_start = -1
for i, line in enumerate(lines):
    if "} else if (action.command === 'wiki') {" in line:
        wiki_start = i
        break

if wiki_start != -1:
    end_idx = -1
    for i in range(wiki_start, len(lines)):
        if "} else {" in lines[i] and "Executed" in lines[i+1]:
            end_idx = i
            break
    
    if end_idx != -1:
        new_block = '''        } else if (action.command === 'wiki') {
          if (result && result.results) {
            if (result.results.length === 0) {
              this.appendMessage('agent', 'Tala', `No wiki nodes found matching search.`);
            } else {
              const nodesList = result.results.map(n => `• [${n.id}] ${n.title} (Relevance: ${n.score.toFixed(2)})`).join('\\n');
              this.appendMessage('agent', 'Tala', `Wiki Search Results:\\n${nodesList}`);
            }
          } else {
            this.appendMessage('agent', 'Tala', `Failed to query wiki: ${JSON.stringify(result)}`);
          }
'''
        lines = lines[:wiki_start] + [new_block] + lines[end_idx:]
        with open('renderer/renderer.js', 'w', encoding='utf-8') as f:
            f.writelines(lines)
        print('Fixed wiki block')
    else:
        print('Could not find end of wiki block')
else:
    print('Could not find start of wiki block')
