"""Serve the presentation with live reload.

The page polls `/__mtime` and reloads itself when the served files change.
With `--rev`, files are read from a jj revision of `--repo` instead of from disk, so the host can
serve a container workspace's snapshots (`t.cld@`) without a checkout of it.
"""

import argparse
import functools
import http.server
import mimetypes
import subprocess
from pathlib import Path

PREFIX = "presentation"


def _jj(repo: str, *args: str) -> bytes:
    return subprocess.run(
        ["jj", "-R", repo, "--ignore-working-copy", *args], capture_output=True, check=True
    ).stdout


class _DiskHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, root: Path, **kwargs: object) -> None:
        self.root = root
        super().__init__(*args, directory=str(root), **kwargs)  # type: ignore[arg-type]

    def version(self) -> bytes:
        return str(max(path.stat().st_mtime_ns for path in self.root.rglob("*") if path.is_file())).encode()

    def do_GET(self) -> None:
        if self.path != "/__mtime":
            return super().do_GET()
        self.send_body(self.version(), "text/plain")

    def send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        if "/__mtime" not in str(args[0]):
            super().log_message(format, *args)


class _StoreHandler(_DiskHandler):
    def __init__(self, *args: object, repo: str, rev: str, **kwargs: object) -> None:
        self.repo, self.rev = repo, rev
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def version(self) -> bytes:
        return _jj(self.repo, "log", "--no-graph", "-r", self.rev, "-T", "commit_id")

    def do_GET(self) -> None:
        if self.path == "/__mtime":
            return self.send_body(self.version(), "text/plain")
        rel = self.path.split("?")[0].split("#")[0].lstrip("/") or "index.html"
        if ".." in rel.split("/"):
            return self.send_error(400)
        try:
            body = _jj(self.repo, "file", "show", "-r", self.rev, f"{PREFIX}/{rel}")
        except subprocess.CalledProcessError:
            return self.send_error(404)
        self.send_body(body, mimetypes.guess_type(rel)[0] or "application/octet-stream")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo", default=".", help="jj repo to read from, with --rev")
    parser.add_argument("--rev", help="serve files from this jj revision instead of from disk")
    args = parser.parse_args()
    if args.rev:
        handler = functools.partial(_StoreHandler, repo=args.repo, rev=args.rev, root=Path("."))
        source = f"{args.repo} at {args.rev}"
    else:
        root = Path(__file__).resolve().parent
        handler = functools.partial(_DiskHandler, root=root)
        source = str(root)
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as server:
        print(f"Serving {source} on http://{args.host}:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
