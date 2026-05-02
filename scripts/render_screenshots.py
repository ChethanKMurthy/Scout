"""Render the latest scout run into colourful SVGs for the README."""
from __future__ import annotations
import glob
import json
import os
from pathlib import Path

from rich.console import Console
from rich.text import Text


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets"
OUT.mkdir(exist_ok=True)


def _latest(pattern: str) -> Path:
    files = sorted(glob.glob(str(ROOT / pattern)), key=os.path.getmtime)
    if not files:
        raise SystemExit(f"no files match {pattern}")
    return Path(files[-1])


def render_top5() -> None:
    data = json.loads(_latest("scout_output/*_top5.json").read_text())
    console = Console(record=True, width=110)
    console.print()
    console.rule(
        "[bold magenta]OSS Contribution Scout — Top 5 picks[/]",
        style="bright_magenta",
    )
    console.print()
    for entry in data.get("top", [])[:5]:
        header = Text()
        header.append(f"  #{entry.get('rank')}", style="bold green")
        header.append("  ", style="")
        header.append(entry.get("repo", ""), style="bold cyan")
        header.append("#", style="dim")
        header.append(str(entry.get("issue", "")), style="bold yellow")
        console.print(header)
        console.print(f"     [white]{entry.get('title','')}[/]")
        console.print(f"     [dim]→[/] {entry.get('why_pick_this','')}")
        console.print(f"     [bold blue]▶ first step:[/] {entry.get('first_step','')}")
        console.print()
    console.save_svg(str(OUT / "top5.svg"), title="scout.py — top 5")


def render_drops() -> None:
    drops = [
        ("ruff",     "rival PRs",         11),
        ("uv",       "marked_as_duplicate", 12),
        ("pydantic", "rival PRs / dupes",  6),
        ("polars",   "marked_as_duplicate", 3),
        ("fastapi",  "rival PRs",          2),
        ("pytest",   "marked_as_duplicate", 1),
        ("poetry",   "rival PRs",          1),
    ]
    total = sum(n for _, _, n in drops)

    console = Console(record=True, width=110)
    console.print()
    console.rule(
        "[bold red]Issues filtered by the maintainer-rejection vetting layer[/]",
        style="bright_red",
    )
    console.print()
    console.print(
        f"  [bold]{total} issues dropped[/] across 8 repos that the first version would have shown to the LLM.\n"
    )
    max_n = max(n for _, _, n in drops)
    for repo, reason, n in drops:
        bar = "█" * int(round(n / max_n * 30))
        line = Text()
        line.append(f"  {repo:<10}", style="bold cyan")
        line.append(f"{n:>3} ", style="bold yellow")
        line.append(bar, style="red")
        line.append(f"  {reason}", style="dim")
        console.print(line)
    console.print()
    console.save_svg(str(OUT / "drops.svg"), title="vetting — issues dropped")


if __name__ == "__main__":
    render_top5()
    render_drops()
    print("wrote", OUT / "top5.svg")
    print("wrote", OUT / "drops.svg")
