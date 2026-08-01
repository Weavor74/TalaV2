# File: `plugin.json`

## Overview

This is a structured configuration file.

## File Contents

```json
{
  "id": "tala.test.mcp",
  "name": "Test MCP Provider",
  "version": "1.0.0",
  "entrypoint": "node dist/index.js",
  "trust_level": "standard",
  "permissions": ["fs:read", "shell:execute"],
  "tools": ["test_echo"],
  "mcp_servers": []
}

```
