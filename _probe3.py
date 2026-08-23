import sys, tempfile, subprocess
sys.path.insert(0, "src")
from pathlib import Path
def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "NUL", "GIT_CONFIG_SYSTEM": "NUL"})
tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name) / "repo"
root.mkdir()
_git(root, "init", "-b", "main")
_git(root, "config", "user.email", "t@example.com")
_git(root, "config", "user.name", "t")
enc = "utf-8"
def write(text):
    (root / "README.md").write_text(text, encoding=enc)
write("# D22\n")
_git(root, "add", "README.md")
_git(root, "commit", "-m", "base")
# 场景1：CRLF-only（内容文本一致）
write("# D22\r\n")
r = _git(root, "diff", "--quiet", "--ignore-space-at-eol", "--no-ext-diff", "--no-textconv", "--no-color", "--", "README.md")
print("CRLF-only exit(ignore-space-at-eol):", r.returncode)
# 场景2：真实文本改动 + CRLF
write("# D22 changed\r\n")
r2 = _git(root, "diff", "--quiet", "--ignore-space-at-eol", "--no-ext-diff", "--no-textconv", "--no-color", "--", "README.md")
print("real-change exit:", r2.returncode)
