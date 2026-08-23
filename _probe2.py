import sys, tempfile, subprocess
sys.path.insert(0, "src")
from pathlib import Path
def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name) / "repo"
root.mkdir()
_git(root, "init", "-b", "main")
_git(root, "config", "user.email", "t@example.com")
_git(root, "config", "user.name", "t")
(root / "README.md").write_text("# D22\n", encoding="utf-8")
_git(root, "add", "README.md")
_git(root, "commit", "-m", "base")
r = _git(root, "diff", "--quiet", "--no-ext-diff", "--no-textconv", "--no-color", "--", "README.md")
print("diff --quiet exit:", r.returncode, "out:", repr(r.stdout[:60]), "err:", repr(r.stderr[:120]))
r2 = _git(root, "status", "--porcelain", "-z", "--untracked-files=all")
print("status:", repr(r2.stdout))
r3 = _git(root, "diff", "--numstat", "--", "README.md")
print("numstat:", repr(r3.stdout), r3.returncode)
print("bytes:", list((root / "README.md").read_bytes()[:20]))
print("autocrlf global:", _git(root, "config", "--global", "--get", "core.autocrlf").stdout.strip())
print("autocrlf local:", _git(root, "config", "--get", "core.autocrlf").stdout.strip())
