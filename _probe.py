import sys, tempfile
sys.path.insert(0, "src")
from pathlib import Path
import subprocess
def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)
tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name) / "repo"
root.mkdir()
_git(root, "init", "-b", "main")
_git(root, "config", "user.email", "t@example.com")
_git(root, "config", "user.name", "t")
(root / "README.md").write_text("# D22\n", encoding="utf-8")
_git(root, "add", "README.md")
_git(root, "commit", "-m", "base")
from koawa_agent_v2.tools.workspace import WorkspacePathResolver
from koawa_agent_v2.verification.git import GitFacade
resolver = WorkspacePathResolver(root)
facade = GitFacade(root, resolver)
print("baseline entries:", [(e.status, e.path) for e in facade._baseline.entries])
print("protected:", facade.protected_paths)
raw = facade._git_command(("ls-files", "-s", "-z"), max_bytes=1000000)
print("ls-files raw:", raw)
for field in raw.split(b"\x00"):
    if field:
        print("field:", field)
        header, _, path = field.decode("utf-8", "replace").partition("\t")
        print("  header parts:", repr(header.split()), "path:", repr(path))
print("hash-object:", facade._git_command(("hash-object", "README.md"), max_bytes=128))
print("index sha:", facade._git_command(("ls-files", "-s", "README.md"), max_bytes=128))
