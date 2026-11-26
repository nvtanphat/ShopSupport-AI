"""Portfolio demo assets from the real application: one screenshot, one WebP, one short GIF.

    uv run python scripts/portfolio_capture/run.py [--keep-work] [--order SL-DEMO02]

Starts a throwaway PostgreSQL container, migrates and seeds it with the project's own demo data, runs the API (real
Gemini, real retrieval models from .env and .cache) and the Vite dev server, drives the customer UI with Playwright
through one cancellation (message, proposal, Confirm, status updated), then cuts the outputs with ffmpeg:

    portfolio-assets/project-2@2x.png   the proposal moment (customer has not confirmed yet), 2400x2000
    portfolio-assets/project-2.webp     the same image, quality 85, under 400 KB
    portfolio-assets/project-2.gif      6-10 s, 12 fps, 900 px wide, under 3 MB

Needs: Docker running, ffmpeg and ffprobe on PATH, Node with the frontend dependencies and Playwright's Chromium,
GEMINI_API_KEY in .env. The demo customer and order are the seed fixtures (customer1@example.com, SL-DEMO02: paid and
confirmed, so the customer may cancel it themselves). The seed is rebuilt on every run, so the order is cancellable
each time. Nothing from .env is printed; log excerpts shown on failure are redacted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "portfolio-assets"
FRONTEND = ROOT / "frontend"

CONTAINER = "shopagent-portfolio-pg"
PG_PORT, API_PORT, WEB_PORT = 55433, 8013, 5175
# 127.0.0.1, not localhost: the container listens on IPv4 only, and on Windows "localhost" tries IPv6 first, which can
# outlast the API's 5-second checkpoint pool start-up.
DB_URL = f"postgresql+psycopg://shopagent:shopagent_password@127.0.0.1:{PG_PORT}/shopagent_portfolio"
CUSTOMER = ("customer1@example.com", "pass123")  # seed fixture, see docs/demo-data.md

PNG, WEBP, GIF = ASSETS / "project-2@2x.png", ASSETS / "project-2.webp", ASSETS / "project-2.gif"
WEBP_LIMIT, GIF_LIMIT = 400 * 1024, 3 * 1024 * 1024
GIF_SECONDS = (6.0, 10.0)
WINDOWS = sys.platform == "win32"


def say(message: str) -> None:
    print(f"[portfolio] {message}", flush=True)


def redact(text: str) -> str:
    """Long token-like strings are never shown, whatever they are."""
    return re.sub(r"[A-Za-z0-9_\-.]{32,}", "<redacted>", text)


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT, log: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if log is not None:
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if check and result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-25:])
        raise SystemExit(f"command failed ({' '.join(cmd[:3])} ...):\n{redact(tail)}")
    return result


def preflight() -> None:
    problems = []
    if run(["docker", "info"], check=False).returncode != 0:
        problems.append("Docker is not running")
    for tool in ("ffmpeg", "ffprobe", "node", "uv"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} is not on PATH")
    if not (FRONTEND / "node_modules" / "@playwright" / "test").exists():
        problems.append("frontend dependencies are missing: npm --prefix frontend install")
    env_file = ROOT / ".env"
    has_key = env_file.exists() and re.search(r"^GEMINI_API_KEY=\s*\S+", env_file.read_text(encoding="utf-8"), re.M)
    if not has_key:
        problems.append("GEMINI_API_KEY is not set in .env")
    if problems:
        raise SystemExit("missing prerequisites:\n  - " + "\n  - ".join(problems))


def wait_for(url: str, what: str, seconds: int) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(2)
    raise SystemExit(f"{what} did not become ready at {url} within {seconds}s")


def stop_tree(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:
        process.terminate()


def start_postgres() -> None:
    run(["docker", "rm", "-f", CONTAINER], check=False)
    run([
        "docker", "run", "-d", "--rm", "--name", CONTAINER,
        "-e", "POSTGRES_USER=shopagent", "-e", "POSTGRES_PASSWORD=shopagent_password", "-e", "POSTGRES_DB=shopagent_portfolio",
        "-p", f"127.0.0.1:{PG_PORT}:5432", "pgvector/pgvector:pg17",
    ])
    for _ in range(60):
        if run(["docker", "exec", CONTAINER, "pg_isready", "-U", "shopagent", "-d", "shopagent_portfolio"], check=False).returncode == 0:
            time.sleep(2)  # the server restarts once after initialisation
            return
        time.sleep(1)
    raise SystemExit("PostgreSQL did not become ready")


def prepare_data(work: Path) -> None:
    env = {"DATABASE_URL": DB_URL, "DATABASE_URL_SYNC": DB_URL, "PYTHONUTF8": "1"}
    say("migrating and seeding the demo database")
    run(["uv", "run", "alembic", "upgrade", "head"], env=env, log=work / "migrate.log")
    run(["uv", "run", "python", "-m", "shopagent.cli", "seed", "--now", datetime.now().astimezone().isoformat(timespec="seconds")], env=env, log=work / "seed.log")


def start_api(work: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, "DATABASE_URL": DB_URL, "DATABASE_URL_SYNC": DB_URL, "PYTHONUTF8": "1"}
    cmd = ["uv", "run", "--extra", "rerank", "uvicorn", "shopagent.api.app:app", "--port", str(API_PORT)]
    if WINDOWS:  # psycopg's async driver does not run on the default Proactor loop
        cmd += ["--loop", "asyncio:SelectorEventLoop"]
    return subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=(work / "api.log").open("wb"), stderr=subprocess.STDOUT)


def start_web(work: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, "API_PROXY_TARGET": f"http://127.0.0.1:{API_PORT}"}
    cmd = [shutil.which("npx") or "npx", "vite", "--port", str(WEB_PORT), "--strictPort"]
    return subprocess.Popen(cmd, cwd=FRONTEND, env=env, stdout=(work / "web.log").open("wb"), stderr=subprocess.STDOUT)


def capture(work: Path, order: str) -> dict[str, float | str]:
    say("driving the customer UI (one real Gemini turn)")
    env = {
        "NODE_PATH": str(FRONTEND / "node_modules"), "WEB_URL": f"http://localhost:{WEB_PORT}", "ORDER_ID": order,
        "CUSTOMER_EMAIL": CUSTOMER[0], "CUSTOMER_PASSWORD": CUSTOMER[1],
    }
    result = run(["node", str(Path(__file__).with_name("capture.cjs")), str(work), str(PNG)], env=env, check=False)
    print(redact(result.stdout.strip()))
    if result.returncode != 0:
        print(redact(result.stderr.strip()))
        raise SystemExit(f"capture failed; see {work / 'failure.png'}")
    marks: dict[str, float | str] = json.loads((work / "marks.json").read_text(encoding="utf-8"))
    return marks


def probe(path: Path) -> dict[str, float | int]:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)]).stdout
    data = json.loads(out)
    return {"width": data["streams"][0]["width"], "height": data["streams"][0]["height"], "seconds": float(data["format"].get("duration", 0))}


def make_webp() -> None:
    for quality in (85, 80, 75, 70):
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(PNG), "-c:v", "libwebp", "-quality", str(quality), "-compression_level", "6", str(WEBP)])
        if WEBP.stat().st_size <= WEBP_LIMIT:
            say(f"webp written at quality {quality}")
            return
    raise SystemExit(f"webp stays above {WEBP_LIMIT // 1024} KB even at quality 70 ({WEBP.stat().st_size // 1024} KB)")


def dark_frames(video: Path, threshold: float = 40.0) -> list[float]:
    """Times of frames whose bottom-right quarter is dark.

    The recorder now and then emits a frame with the page shrunk into the top-left of a black canvas (seen with
    deviceScaleFactor 2). A healthy frame is pale there, so a dark quarter marks a bad frame.
    """
    graph = f"movie={video.name},crop=iw/4:ih/4:iw*3/4:ih*3/4,signalstats"
    out = run(
        ["ffprobe", "-v", "error", "-f", "lavfi", "-i", graph, "-show_entries", "frame=pts_time:frame_tags=lavfi.signalstats.YAVG", "-of", "csv=p=0"],
        cwd=video.parent,
    ).stdout
    times = []
    for line in out.splitlines():
        numbers = [part for part in line.split(",") if part.strip()]
        if len(numbers) >= 2 and float(numbers[-1]) < threshold:
            times.append(float(numbers[0]))
    return times


def without_frames(times: list[float], tolerance: float = 0.015) -> str:
    """A select filter that drops the frames at `times`; the fps filter later repeats the previous frame in the gap."""
    if not times:
        return "null"
    return "select='not(" + "+".join(f"between(t,{t - tolerance:.3f},{t + tolerance:.3f})" for t in times) + ")'"


def segments(marks: dict[str, float]) -> list[tuple[float, float, float]]:
    """(start, end, speed) pieces of the recording: typing, a fast-forwarded wait, the proposal, the confirmation."""
    typing = (marks["typeStart"] - 0.5, marks["sent"] + 0.3, 1.0)
    wait_start, wait_end = marks["sent"] + 0.3, marks["proposalVisible"] - 0.4
    wait = (wait_start, wait_end, max(1.0, (wait_end - wait_start) / 1.4))
    hold = (marks["proposalVisible"] - 0.4, marks["confirmClicked"] - 0.1, 1.0)
    done = (marks["confirmClicked"] - 0.1, min(marks["cancelledVisible"] + 1.4, marks["end"]), 1.0)
    pieces = [typing, wait, hold, done]
    total = sum((e - s) / v for s, e, v in pieces)
    if total > GIF_SECONDS[1] - 0.3:  # shorten the hold on the proposal first
        excess = total - (GIF_SECONDS[1] - 0.3)
        hold = (hold[0] + min(excess, (hold[1] - hold[0]) - 1.5), hold[1], 1.0)
        pieces = [typing, wait, hold, done]
    return pieces


def make_gif(video: Path, marks: dict[str, float]) -> None:
    pieces = segments(marks)
    glitched = dark_frames(video)
    if len(glitched) > 20:
        raise SystemExit(f"{len(glitched)} frames of the recording are glitched: record again")
    if glitched:
        say(f"dropping {len(glitched)} glitched recorder frame(s) at {', '.join(f'{t:.2f}s' for t in glitched)}")
    clean = without_frames(glitched)
    parts = [f"[0:v]{clean},trim=start={s:.3f}:end={e:.3f},setpts=(PTS-STARTPTS)/{v:.3f}[v{i}]" for i, (s, e, v) in enumerate(pieces)]
    joined = "".join(f"[v{i}]" for i in range(len(pieces)))
    for colors, scale in ((128, 900), (96, 900), (64, 900)):
        graph = ";".join(parts) + (
            f";{joined}concat=n={len(pieces)}:v=1:a=0,fps=12,scale={scale}:-1:flags=lanczos,split[a][b];"
            f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
        )
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-filter_complex", graph, "-loop", "0", str(GIF)])
        if GIF.stat().st_size <= GIF_LIMIT:
            say(f"gif written with {colors} colours")
            if dark_frames(GIF):
                raise SystemExit("the gif still contains glitched frames")
            return
    raise SystemExit(f"gif stays above {GIF_LIMIT // 1024 // 1024} MB ({GIF.stat().st_size // 1024} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--order", default="SL-DEMO02", help="seed order the customer may cancel")
    parser.add_argument("--keep-work", action="store_true", help="keep the temporary video, logs and marks")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a code page without Vietnamese
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    preflight()
    ASSETS.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="portfolio_capture_"))
    api = web = None
    try:
        say("starting PostgreSQL")
        start_postgres()
        prepare_data(work)
        say("starting the API and the web server")
        api, web = start_api(work), start_web(work)
        wait_for(f"http://127.0.0.1:{API_PORT}/readyz", "API", 240)
        wait_for(f"http://localhost:{WEB_PORT}", "web server", 60)
        marks = capture(work, args.order)
        say("encoding")
        make_webp()
        make_gif(Path(str(marks["video"])), {k: float(v) for k, v in marks.items() if k != "video"})
    finally:
        stop_tree(web)
        stop_tree(api)
        run(["docker", "rm", "-f", CONTAINER], check=False)
        if args.keep_work:
            say(f"work files kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    for path in (PNG, WEBP, GIF):
        info = probe(path)
        extra = f", {info['seconds']:.1f} s" if path == GIF else ""
        say(f"{path.relative_to(ROOT)}: {path.stat().st_size / 1024:.0f} KB, {info['width']}x{info['height']}{extra}")


if __name__ == "__main__":
    main()
