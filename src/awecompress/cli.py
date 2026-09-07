"""CLI: serve / status / config / clear.

`serve` runs in the foreground (Ctrl-C stops it). Backgrounding is the
user's process manager — v1 ships no daemon machinery.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import aiohttp
import click

from awecompress import __version__
from awecompress.config import config_path, db_path, load_config
from awecompress.server import serve as serve_proxy
from awecompress.store import Store


@click.group(name="awecompress", context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-v", "--version", message="awecompress %(version)s")
def cli() -> None:
    """Compress long coding-agent context before it reaches your provider."""


@cli.command()
@click.option("--port", type=int, default=None, help="Listen port (default: config port or 8808).")
@click.option("--host", default=None, help="Listen address (default: 127.0.0.1).")
@click.option("--upstream", default=None, help="Upstream base URL (default: config upstream).")
@click.option("--config", "config_file", type=click.Path(), default=None,
              help="Config file path (default: ~/.config/awecompress/config.json).")
def serve(port: int, host: str, upstream: str, config_file: str) -> None:
    """Run the compression proxy in the foreground."""
    cfg = load_config(Path(config_file).expanduser() if config_file else None)
    if upstream:
        cfg = replace(cfg, upstream=upstream)
    store = Store(cfg.db_path)
    try:
        asyncio.run(serve_proxy(cfg, store, port, host))
    except KeyboardInterrupt:
        pass


@cli.command()
@click.option("--config", "config_file", type=click.Path(), default=None,
              help="Config file path (default: ~/.config/awecompress/config.json).")
def status(config_file: str) -> None:
    """Show running state, config, and compression stats."""
    cfg = load_config(Path(config_file).expanduser() if config_file else None)
    url = f"http://{cfg.host}:{cfg.port}/"
    running = False
    try:
        async def probe():
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    return await resp.json(content_type=None)
        running = asyncio.run(probe()).get("service") == "awecompress"
    except Exception:
        pass

    store = Store(cfg.db_path)
    stats = store.stats()
    store.close()
    click.echo(f"awecompress {__version__}")
    click.echo(f"  proxy     : {'running at ' + url if running else 'not running'}")
    click.echo(f"  upstream  : {cfg.upstream}")
    click.echo(f"  compress  : above {cfg.threshold_tokens} est. tokens, "
               f"keep last {cfg.keep_recent_turns} turns, min span {cfg.min_span_tokens}")
    click.echo(f"  summaries : {stats['sessions']} sessions, {stats['calls']} summary calls, "
               f"~{stats['saved_tokens']} tokens saved")
    click.echo(f"  store     : {cfg.db_path}")


@cli.group()
def config() -> None:
    """Show the config file."""


@config.command("path")
def config_path_cmd() -> None:
    """Print the config file path."""
    click.echo(str(config_path()))


@config.command("show")
def config_show() -> None:
    """Print the config file contents."""
    path = config_path()
    if not path.exists():
        click.echo(f"(no config file at {path} — defaults apply; run 'awecompress serve' to write one)")
        return
    click.echo(path.read_text().rstrip())


@cli.command()
@click.option("--yes", is_flag=True, help="Delete without asking.")
def clear(yes: bool) -> None:
    """Delete stored summaries (sessions start uncompressed)."""
    path = Path(db_path())
    if not path.exists():
        click.echo("nothing to clear — no summary store yet")
        return
    if not yes:
        click.confirm(f"delete {path} (all frozen summaries)?", abort=True)
    for suffix in ("", "-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.unlink()
    click.echo(f"deleted {path}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
