#!/usr/bin/env python3
"""Contribution Scout: find bounded, resume-worthy OSS contributions."""
from __future__ import annotations
import argparse, asyncio, base64, json, logging, os, re, sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx
from anthropic import APIError, APIStatusError, AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-6"
JUDGE = "claude-opus-4-7"
GH = "https://api.github.com"

DEFAULT_REPOS = [
    "pydantic/pydantic",
    "astral-sh/ruff",
    "astral-sh/uv",
    "pola-rs/polars",
    "encode/httpx",
    "tiangolo/fastapi",
    "pytest-dev/pytest",
    "python-poetry/poetry",
]

INCLUDE = {"help wanted", "help-wanted", "bug", "performance", "perf",
           "enhancement", "feature", "type-stubs", "typing"}
EXCLUDE = {"good first issue", "good-first-issue", "easy", "beginner",
           "documentation", "docs", "duplicate", "wontfix", "invalid",
           "question", "stale", "needs-triage", "needs triage", "discussion",
           "needs-decision", "deferred", "not-planned", "blocked", "roadmap"}
MAINTAINER_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}
RIVAL_PR_LOOKBACK_DAYS = 180

SCOUT_PROMPT = """You are a senior engineer triaging OSS issues to identify the BEST contributions for a candidate trying to add 2–3 strong items to their resume.

For the supplied repository, pick UP TO 3 issues from the provided list that best match these criteria:

MUST HAVE
- Bounded: a competent dev can ship a PR in 1–2 weeks of part-time work.
- Substantive: bug fix with a clear regression test, perf improvement with a benchmark, type/correctness fix, or a small but real feature. NOT docs, formatting, lint, dep bumps, or "good first issue" tier work.
- Mergeable signal: recent maintainer activity in the issue, clear acceptance criteria, no architectural debate stalled for months.
- Defensible on a resume: the candidate can describe the problem, the fix, and the impact in 2–3 sentences during an interview.

REJECT IMMEDIATELY (do not pick) if any of:
- A maintainer comment (marked `[maintainer]`) says the change is off-roadmap, won't be accepted, deferred, or that the obvious fix is wrong.
- The thread shows the maintainers want a different architectural direction than the issue title suggests.
- A maintainer is signalling "we'll do this differently / upstream / later" rather than "yes, send a PR".
- A prior closed-unmerged PR by another contributor (flagged as `rival_pr_closed_unmerged`) attempted the obvious fix and was rejected, unless the issue body or maintainer comments explicitly invite a different approach.

PREFER
- Issues with reproducible failing test cases or clear benchmarks.
- Issues where the fix touches non-trivial code paths (parsing, scheduling, caching, async, type system, query planning, etc.) — something interview-worthy.
- Issues where the maintainers have indicated the direction of the fix.

OUTPUT — strict JSON, no fences, no preamble:
{
  "repo": "owner/name",
  "picks": [
    {
      "issue_number": 1234,
      "title": "exact title from input",
      "why_this_one": "2–3 sentences: what's wrong, what evidence shows it's tractable, why a maintainer will review it.",
      "approach": "3–5 bullet points outlining the implementation strategy.",
      "effort": "small | medium | large",
      "skills_demonstrated": ["e.g. async python", "type system", "perf profiling"],
      "resume_blurb": "Single sentence the candidate could put on a resume after merge."
    }
  ]
}

If no issue clears the bar, return {"repo": "...", "picks": []} and nothing else."""

JUDGE_PROMPT = """You are a tech lead helping a junior engineer pick which OSS contributions to actually pursue.

Given a list of candidate issues across multiple repos, rank the top 5 by:
1. Tractability (will it actually merge in <2 weeks of part-time work?)
2. Resume value (repo recognition × technical substance)
3. Interview narrative (can they tell a clear story about the work?)

Output ONLY JSON, no fences:
{"top": [{"rank":1, "repo":"...", "issue":1234, "title":"...", "why_pick_this":"...", "first_step":"concrete first action the candidate should take today"}]}"""

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scout")


@dataclass
class Repo:
    name: str
    stars: int
    language: str | None
    open_issues: int
    pushed_recent: bool
    merge_signal: float
    median_review_days: float | None
    issues: list
    error: str | None = None


@dataclass
class Picks:
    repo: str
    raw: str
    parsed: dict


def trim(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n...[truncated {len(s)-n}]"


class GH_:
    def __init__(self, token, conc=8):
        self.c = httpx.AsyncClient(base_url=GH, timeout=30.0, follow_redirects=True, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "scout/1.0",
        })
        self.s = asyncio.Semaphore(conc)

    async def close(self):
        await self.c.aclose()

    async def _g(self, path, params=None):
        async with self.s:
            for a in range(4):
                try:
                    r = await self.c.get(path, params=params)
                except httpx.HTTPError:
                    if a == 3: raise
                    await asyncio.sleep(2 ** a); continue
                if r.status_code == 200: return r.json()
                if r.status_code == 404: return None
                if r.status_code in (403, 429):
                    await asyncio.sleep(min(60, 2 ** (a + 1))); continue
                r.raise_for_status()
            return None

    async def meta(self, repo):
        return await self._g(f"/repos/{repo}")

    async def issues(self, repo):
        items = await self._g(f"/repos/{repo}/issues", params={
            "state": "open", "sort": "comments", "direction": "desc", "per_page": 80,
        }) or []
        return [i for i in items if "pull_request" not in i]

    async def recent_prs(self, repo, days=30):
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return await self._g(f"/repos/{repo}/pulls", params={
            "state": "closed", "sort": "updated", "direction": "desc", "per_page": 50,
        }) or []

    async def issue_events(self, repo, num):
        return await self._g(f"/repos/{repo}/issues/{num}/events", params={"per_page": 100}) or []

    async def issue_timeline(self, repo, num):
        return await self._g(f"/repos/{repo}/issues/{num}/timeline", params={"per_page": 100}) or []

    async def issue_comments(self, repo, num):
        return await self._g(f"/repos/{repo}/issues/{num}/comments", params={"per_page": 100}) or []

    async def issue(self, repo, num):
        return await self._g(f"/repos/{repo}/issues/{num}")


def filter_issues(issues, max_keep=20):
    """Keep substantive issues with INCLUDE labels and recent activity."""
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=120)
    for i in issues:
        labels = {l["name"].lower() for l in i.get("labels", [])}
        if labels & EXCLUDE:
            continue
        if not (labels & INCLUDE) and i.get("comments", 0) < 3:
            continue
        try:
            updated = datetime.fromisoformat(i["updated_at"].replace("Z", "+00:00"))
            if updated < cutoff:
                continue
        except Exception:
            pass
        out.append({
            "number": i["number"],
            "title": i["title"],
            "comments": i.get("comments", 0),
            "labels": sorted(labels),
            "updated_at": i.get("updated_at"),
            "body": trim((i.get("body") or "").strip(), 700),
            "url": i.get("html_url"),
        })
        if len(out) >= max_keep:
            break
    return out


REF_RE = re.compile(r'(?:#|/(?:pull|issues)/)(\d+)\b')


async def vet_issue(gh: GH_, repo: str, issue: dict) -> tuple[dict | None, str]:
    """Deep-check one issue. Drop on hard maintainer-rejection signals; otherwise
    annotate with maintainer comments for the LLM to weigh."""
    num = issue["number"]
    timeline, comments = await asyncio.gather(
        gh.issue_timeline(repo, num), gh.issue_comments(repo, num)
    )

    for e in timeline:
        if e.get("event") == "marked_as_duplicate":
            actor = (e.get("actor") or {}).get("login", "?")
            return None, f"marked_as_duplicate by {actor}"

    refs: set[int] = set()
    for src in [issue.get("body") or ""] + [(c.get("body") or "") for c in comments]:
        for m in REF_RE.finditer(src):
            try:
                refs.add(int(m.group(1)))
            except (ValueError, TypeError):
                pass
    refs.discard(num)
    refs_list = list(refs)[:10]

    if refs_list:
        ref_data = await asyncio.gather(*[gh.issue(repo, n) for n in refs_list])
        for n, d in zip(refs_list, ref_data):
            if not d or not d.get("pull_request"):
                continue
            if d.get("state") != "closed":
                continue
            if d["pull_request"].get("merged_at"):
                continue
            pr_events = await gh.issue_events(repo, n)
            closer = next(((e.get("actor") or {}).get("login")
                           for e in pr_events if e.get("event") == "closed"), None)
            author = (d.get("user") or {}).get("login")
            if closer and closer != author:
                return None, f"rival PR #{n} closed by {closer} (not author {author})"

    maint = []
    for c in comments:
        if c.get("author_association") in MAINTAINER_ASSOC:
            maint.append({
                "user": c["user"]["login"],
                "at": (c.get("created_at") or "")[:10],
                "assoc": c["author_association"],
                "body": trim((c.get("body") or "").strip(), 500),
            })
    issue["maintainer_comments"] = maint[-3:]
    return issue, ""


async def fetch(gh: GH_, repo: str) -> Repo:
    log.info(f"📥 {repo}")
    try:
        meta, issues, prs = await asyncio.gather(
            gh.meta(repo), gh.issues(repo), gh.recent_prs(repo)
        )
        if not meta:
            return Repo(repo, 0, None, 0, False, 0.0, None, [], "not found")
        merged = sum(1 for p in prs if p.get("merged_at"))
        merge_signal = merged / max(1, len(prs))

        spans = []
        for p in prs:
            if p.get("merged_at"):
                try:
                    c = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
                    m = datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))
                    spans.append((m - c).total_seconds() / 86400)
                except Exception:
                    pass
        spans.sort()
        median = spans[len(spans) // 2] if spans else None

        pushed = False
        if meta.get("pushed_at"):
            try:
                p = datetime.fromisoformat(meta["pushed_at"].replace("Z", "+00:00"))
                pushed = (datetime.now(timezone.utc) - p).days < 14
            except Exception:
                pass

        prefiltered = filter_issues(issues)
        vetted = await asyncio.gather(*[vet_issue(gh, repo, i) for i in prefiltered])
        kept, dropped = [], []
        for issue, reason in vetted:
            if issue is None:
                dropped.append(reason)
            else:
                kept.append(issue)
        if dropped:
            log.info(f"   {repo}: dropped {len(dropped)} ({'; '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})")

        return Repo(
            name=repo,
            stars=meta.get("stargazers_count", 0),
            language=meta.get("language"),
            open_issues=meta.get("open_issues_count", 0),
            pushed_recent=pushed,
            merge_signal=merge_signal,
            median_review_days=median,
            issues=kept,
        )
    except Exception as e:
        log.error(f"fetch {repo}: {e}")
        return Repo(repo, 0, None, 0, False, 0.0, None, [], str(e))


def render(r: Repo) -> str:
    parts = [
        f"# Repository: {r.name}",
        f"Stars: {r.stars} | Language: {r.language} | Open issues: {r.open_issues}",
        f"Pushed in last 14 days: {r.pushed_recent}",
        f"30-day merged-PR ratio: {r.merge_signal:.2f} | "
        f"Median PR review days: {r.median_review_days}",
        "\n## Candidate issues",
    ]
    if not r.issues:
        parts.append("(none after filtering)")
    for i in r.issues:
        parts.append(
            f"\n### #{i['number']} — {i['title']}\n"
            f"comments={i['comments']} labels={i['labels']} updated={i['updated_at']}\n"
            f"{i['body']}"
        )
        for mc in i.get("maintainer_comments") or []:
            parts.append(
                f"\n  [maintainer {mc['assoc']} · {mc['user']} · {mc['at']}]: {mc['body']}"
            )
    return "\n".join(parts)


def parse_json(s: str) -> dict:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"): s = s[:-3]
    try:
        return json.loads(s.strip())
    except json.JSONDecodeError:
        return {"_raw": s}


async def scout(claude: AsyncAnthropic, r: Repo, sem) -> Picks:
    if r.error or not r.issues:
        return Picks(r.name, "", {"repo": r.name, "picks": [], "skipped": r.error or "no candidate issues"})
    body = render(r)
    log.info(f"🧠 {r.name}")
    async with sem:
        for a in range(3):
            try:
                resp = await claude.messages.create(
                    model=MODEL, max_tokens=2500, temperature=0.2,
                    system=[{"type": "text", "text": SCOUT_PROMPT,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user",
                               "content": f"<repository>\n{body}\n</repository>\n\nReturn the JSON now."}],
                )
                txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
                return Picks(r.name, txt, parse_json(txt))
            except APIStatusError as e:
                if e.status_code in (429, 503, 529) and a < 2:
                    await asyncio.sleep(2 ** (a + 2)); continue
                return Picks(r.name, "", {"repo": r.name, "error": str(e)})
            except APIError as e:
                return Picks(r.name, "", {"repo": r.name, "error": str(e)})
    return Picks(r.name, "", {"repo": r.name, "error": "retries exhausted"})


async def rank_all(claude: AsyncAnthropic, picks: list[Picks]) -> dict:
    flat = []
    for p in picks:
        for it in (p.parsed.get("picks") or []):
            flat.append({"repo": p.repo, **it})
    if not flat:
        return {"top": [], "error": "no candidates"}
    log.info(f"⚖️  ranking {len(flat)} candidate issues")
    r = await claude.messages.create(
        model=JUDGE, max_tokens=3000, system=JUDGE_PROMPT,
        messages=[{"role": "user", "content": json.dumps(flat, indent=2)}],
    )
    raw = "".join(b.text for b in r.content if getattr(b, "type", None) == "text")
    return parse_json(raw)


async def run(repos, out: Path, conc):
    if not os.getenv("GITHUB_TOKEN") or not os.getenv("ANTHROPIC_API_KEY"):
        log.error("Set GITHUB_TOKEN and ANTHROPIC_API_KEY in .env"); sys.exit(1)
    out.mkdir(parents=True, exist_ok=True)
    gh = GH_(os.getenv("GITHUB_TOKEN"), conc)
    cl = AsyncAnthropic()
    sem = asyncio.Semaphore(conc)
    try:
        repos_data = await asyncio.gather(*[fetch(gh, r) for r in repos])
        picks = await asyncio.gather(*[scout(cl, r, sem) for r in repos_data])

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        all_out = [p.parsed for p in picks]
        (out / f"{ts}_per_repo.json").write_text(json.dumps(all_out, indent=2))

        top = await rank_all(cl, picks)
        (out / f"{ts}_top5.json").write_text(json.dumps(top, indent=2))

        print("\n" + "=" * 60 + "\n🎯 TOP 5 CONTRIBUTIONS TO PURSUE\n" + "=" * 60)
        for e in (top.get("top") or []):
            print(f"\n#{e.get('rank')} · {e.get('repo')}#{e.get('issue')}")
            print(f"   {e.get('title')}")
            print(f"   → {e.get('why_pick_this')}")
            print(f"   ▶ first step: {e.get('first_step')}")
        if not top.get("top"):
            print(json.dumps(top, indent=2))

        log.info(f"💾 {out.resolve()}")
    finally:
        await gh.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", nargs="*", default=DEFAULT_REPOS)
    ap.add_argument("--out", type=Path, default=Path("./scout_output"))
    ap.add_argument("--concurrency", type=int, default=5)
    a = ap.parse_args()
    asyncio.run(run(a.repos, a.out, a.concurrency))
