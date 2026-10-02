---
name: github
description: "Research public GitHub repositories — browse files, read code, search repos, view commits, issues, and PRs. No auth needed. Use when you need to explore any public repo's actual source code, not just documentation."
---

# GitHub Public Repository Research

Browse and read actual source code from any public GitHub repository using the REST API. No authentication required (60 requests/hour).

## Quick Reference

All endpoints use `https://api.github.com`. Use `bash_` with curl or `visit_url` (if discovered).

### Browse Repository Structure

```bash
# List root contents
curl -s "https://api.github.com/repos/{owner}/{repo}/contents/" | python3 -c "import sys,json; [print(f\"{'[D]' if x['type']=='dir' else '   '} {x['path']}\" ) for x in json.load(sys.stdin)]"

# List specific directory
curl -s "https://api.github.com/repos/{owner}/{repo}/contents/{path}" | python3 -c "import sys,json; [print(f\"{'[D]' if x['type']=='dir' else '   '} {x['path']}\" ) for x in json.load(sys.stdin)]"

# Specify branch/tag
curl -s "https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
```

### Read File Contents (Raw)

Fastest way to read actual code — bypasses API rate limits:

```bash
# Raw file content (no rate limit, no base64 decoding needed)
curl -s "https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
```

Examples:
```bash
curl -s "https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/src/anthropic/_client.py"
curl -s "https://raw.githubusercontent.com/fastapi/fastapi/master/fastapi/applications.py"
```

### Search Code

```bash
# Search code in a specific repo
curl -s "https://api.github.com/search/code?q={query}+repo:{owner}/{repo}" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for item in data.get('items', [])[:15]:
    print(f\"{item['path']}  (score: {item.get('score',0):.0f})\")
"

# Search with language filter
curl -s "https://api.github.com/search/code?q={query}+repo:{owner}/{repo}+language:python"

# Search across all of GitHub
curl -s "https://api.github.com/search/code?q={query}+language:python&sort=indexed&per_page=10"
```

### Search Repositories

```bash
# Find repos by topic/keyword
curl -s "https://api.github.com/search/repositories?q={query}&sort=stars&per_page=5" | python3 -c "
import sys,json
for r in json.load(sys.stdin).get('items',[])[:5]:
    print(f\"{r['full_name']}  ★{r['stargazers_count']}  {r.get('description','')[:80]}\")
"
```

### View Commits

```bash
# Recent commits
curl -s "https://api.github.com/repos/{owner}/{repo}/commits?per_page=10" | python3 -c "
import sys,json
for c in json.load(sys.stdin):
    print(f\"{c['sha'][:7]} {c['commit']['message'].split(chr(10))[0][:80]}\")
"

# Commit detail (with diff)
curl -s "https://api.github.com/repos/{owner}/{repo}/commits/{sha}" | python3 -c "
import sys,json
c = json.load(sys.stdin)
print(c['commit']['message'])
for f in c.get('files',[]):
    print(f\"  {f['status']} {f['filename']} (+{f['additions']}/-{f['deletions']})\")
"
```

### Issues & PRs

```bash
# List open issues
curl -s "https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=10"

# List PRs
curl -s "https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=10"

# PR files changed
curl -s "https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"
```

### Repository Info

```bash
# Repo metadata (stars, forks, language, description)
curl -s "https://api.github.com/repos/{owner}/{repo}"

# Languages breakdown
curl -s "https://api.github.com/repos/{owner}/{repo}/languages"

# Branches
curl -s "https://api.github.com/repos/{owner}/{repo}/branches"

# Tags/releases
curl -s "https://api.github.com/repos/{owner}/{repo}/releases?per_page=5"
```

### Tree (full recursive file listing)

```bash
# Get full repo tree (all files, one request)
curl -s "https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1" | python3 -c "
import sys,json
tree = json.load(sys.stdin)
for t in tree.get('tree',[]):
    if t['type'] == 'blob':
        print(t['path'])
" | head -100
```

## Tips

- **Prefer `raw.githubusercontent.com`** for reading files — no rate limit, no base64 decoding
- **Use the tree endpoint** to get the full file listing in one API call instead of recursing through directories
- **Rate limit**: 60 req/hr unauthenticated. Check with: `curl -s -I "https://api.github.com/rate_limit"`
- **Default branches**: most repos use `main`, some older ones use `master`. Check repo metadata if unsure
- **Large files**: the contents API won't return files >1MB. Use raw.githubusercontent.com instead
- **Search limitations**: code search requires the repo to have been indexed (most popular repos are)
