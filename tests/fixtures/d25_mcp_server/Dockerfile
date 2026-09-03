# D25 W5 reference integration image.
# Pinned third-party stdio MCP server: @modelcontextprotocol/server-filesystem
#   version: 2026.8.31
#   tarball sha1 (npm dist.shasum): 7f88dfab06d4521a4bf937dccbaeb79d46a3e717
#   upstream: https://github.com/modelcontextprotocol/servers (MIT)
# No network is used at runtime; containers start with --network none.
FROM node:22-alpine
RUN npm install -g @modelcontextprotocol/server-filesystem@2026.8.31 \
 && mkdir -p /data \
 && printf 'koawa-d25-sample\n' > /data/sample.txt
